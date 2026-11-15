# Argo CD の自己管理（Argo CD 自身を charts/argocd で管理する）

## 構成

- Argo CD は `charts/argocd`（argo-cd チャートのラッパー）で管理する。`applications/base/argocd.yaml` が Application（overlay で `prod/values.yaml` / `dev/values.yaml` を追加）
- 設定（argocd-cm / argocd-rbac-cm）は `charts/argocd/values.yaml`（共通）と `charts/argocd/prod/values.yaml`・`dev/values.yaml`（URL・dex など環境ごと）に書く。**`kubectl apply` や `kubectl edit` で直接変更しない**（次の Sync で戻る）
- `argocd-secret`（dex の GitHub clientSecret、server.secretkey）はチャートで作らない（`configs.secret.createSecret: false`）。
  クラスタ構築時に `bootstrap/argocd-dex-secret.yaml`（gitignore 済み）で投入したものをそのまま使う
- **自動同期にしていない。** Argo CD 自身の更新に失敗すると Argo CD が止まり、自分では直せないため、Diff を確認してから手動で Sync する
- 更新は Dependabot が `charts/argocd/Chart.yaml` の argo-cd のバージョンを上げる PR を dev ブランチに作る
- 同期するブランチ: 本番 `release`、開発 `dev`
- 実行場所の表記: **[Mac]** = kubectl / helm を使う端末

---

## 初回の取り込み（2026-11-03 時点の作業）

それまでは `helm install`（argo-cd 9.5.14、values は既定）で入れ、設定は bootstrap の ConfigMap を `kubectl apply` していた。

事前確認（2026-11-03 実施）: `helm template` の結果と稼働中の差分は次の 3 点だけ。
- ConfigMap の `last-applied-configuration` 注釈
- Deployment / StatefulSet の `checksum/cm` 注釈（Argo CD の Pod が 1 回再起動する）
- Helm hook の Job（`argocd-redis-secret-init`。Sync のたびに PreSync で実行される）

設定値とイメージ（v3.4.2）は同じ。

1. コミット・push する。対象: `charts/argocd/`、`applications/base/argocd.yaml`、`applications/base/kustomization.yaml`、`.github/dependabot.yml`
2. Argo CD の画面で root-app が同期され、`argocd` アプリが OutOfSync で現れる
3. `argocd` アプリの **APP DIFF** を開き、上記の差分だけであることを確認してから **SYNC**
4. Argo CD の Pod が入れ替わる（1〜2 分、画面が一時的に切れる）

確認

```bash
# [Mac]
kubectl -n argocd get pods
kubectl -n argocd get cm argocd-cm -o jsonpath='{.data.dex\.config}'     # GitHub の connector が残っている
kubectl -n argocd get app                                                # 全アプリ Synced / Healthy
```

- [ ] argocd アプリ Synced / Healthy
- [ ] GitHub でログインできる
- [ ] 他のアプリが Synced のまま

Helm のリリース情報（`secret/sh.helm.release.v1.argocd.v1`）は消さずに残す。以後 `helm upgrade` は使わない（戻すときだけ使う）。

---

## 更新（Dependabot の PR）

1. PR の差分（argo-cd チャートのバージョン）と、リリースノートを確認する
   - チャート: https://github.com/argoproj/argo-helm/releases
   - Argo CD 本体: https://github.com/argoproj/argo-cd/releases
   - **メジャーバージョン（例: 9 → 10）は argo-helm の README の「Upgrading」を必ず読む**
2. PR を dev にマージ
3. 開発（https://argocd-dev.local）で `argocd` アプリの APP DIFF を確認して SYNC。上の「確認」と同じ項目を確認
4. dev → main → release とマージ
5. 本番（https://argocd.local）で `argocd` アプリの APP DIFF を確認して SYNC。上の「確認」と同じ項目を確認

---

## 戻し方（Sync 後に Argo CD が動かなくなった場合）

Argo CD が自分を直せないので、Mac から helm で前のバージョンに戻す。

```bash
# [Mac] リポジトリのルートで、前のバージョンの Chart.yaml に戻したうえで実行
git checkout <前のコミット> -- charts/argocd
cd charts/argocd
helm dependency build
helm upgrade argocd . -n argocd -f values.yaml -f prod/values.yaml     # 開発は dev/values.yaml
rm -rf charts Chart.lock
```

Argo CD が復旧したら、release（と main・dev）も前のバージョンに戻す（revert の PR）。戻さないと、次の Sync で壊れたバージョンが再び入る。

---

## クラスタを作り直すとき

```bash
# [Mac]
kubectl create namespace argocd
kubectl apply -f bootstrap/argocd-dex-secret.yaml       # argocd-secret（dex の clientSecret）
cd charts/argocd && helm dependency build && helm install argocd . -n argocd -f values.yaml -f prod/values.yaml && rm -rf charts Chart.lock && cd -
kubectl apply -f bootstrap/argocd-repo-secret.yaml      # リポジトリの認証情報
kubectl --context prod apply -f bootstrap/root-app-prod.yaml
```

以後は root-app 経由で `argocd` アプリが作られ、自己管理に入る。
