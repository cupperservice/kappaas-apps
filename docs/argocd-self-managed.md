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

## サーバー証明書（ブラウザの「保護されていない接続」）

argocd-server のサーバー証明書は、cert-manager がクラスタの CA（ClusterIssuer `k8s-ca`、CN `k8s-on-prem-ca`）で発行する（2026-11-22 から）。

- 設定は `charts/argocd/values.yaml` の `argo-cd.server.certificate`（issuer・有効期間 90 日・15 日前に更新）と、環境ごとのホスト名（`prod/values.yaml`: `argocd.local`、`dev/values.yaml`: `argocd-dev.local`）
- Certificate `argocd-server` が Secret `argocd-server-tls` を作り、argocd-server は自動でそれを使う（再起動不要。更新も自動）
- それまでは argocd-server が自動で作る自己署名証明書（`O=Argo CD`、ホスト名が入っていない）だったので、ブラウザで警告が出ていた
- HTTPProxy は TLS パススルーなので、この証明書がそのままブラウザに届く

### Mac で k8s-ca を信頼する（1 回だけ）

ブラウザの警告を消すには、Mac で各クラスタの k8s-ca を信頼する。Chrome・Edge・Safari は macOS のキーチェーンを使う（Firefox は独自の証明書ストアなので別に設定する）。

```bash
# [Mac] 開発（本番は --context prod・prod-ca.crt に読み替える）
kubectl --context dev get secret k8s-ca-secret -n cert-manager -o jsonpath='{.data.tls\.crt}' | base64 -d > ~/k8s-backup/dev-ca.crt
openssl x509 -in ~/k8s-backup/dev-ca.crt -noout -subject -enddate        # CN = k8s-on-prem-ca
sudo security add-trusted-cert -d -r trustRoot -p ssl -k /Library/Keychains/System.keychain ~/k8s-backup/dev-ca.crt
```

- `-p ssl` で、TLS のサーバー証明書の検証にだけ信頼する
- 確認: `echo | openssl s_client -connect 192.168.3.121:443 -servername argocd-dev.local 2>/dev/null | openssl x509 -noout -issuer -ext subjectAltName`（issuer が `CN=k8s-on-prem-ca`、SAN に `argocd-dev.local`）。ブラウザは一度終了（⌘Q）してから開き直す
- **注意**: k8s-ca は IAM Roles Anywhere のトラストアンカーでもある。Mac で信頼すると、そのクラスタで Certificate を作れる人は、Mac が信頼する任意のホスト名の証明書を発行できるようになる。クラスタの管理者権限を持つのが自分だけであることが前提
- 両クラスタの CA は同じ CN（`k8s-on-prem-ca`）なので、キーチェーンアクセスでは有効期限などで見分ける
- クラスタを作り直して CA が変わったら、キーチェーンアクセスで古い CA を削除してから入れ直す

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
kubectl --context prod apply -f bootstrap/root-app-prod.yaml
# external-secrets が入って CRD ができてから（kubectl get crd secretstores.external-secrets.io）
kubectl --context prod apply -f bootstrap/argocd-secrets.yaml   # SecretStore argocd-secret-store（リポジトリの認証情報を Secrets Manager から取る）
```

以後は root-app 経由で `argocd` アプリが作られ、自己管理に入る。

- リポジトリの認証情報（kappaas-apps・kakeibon-apps）は `argocd-config/repositories` の ExternalSecret が作る。手で apply しない
- kappaas-apps は public なので、認証情報ができる前でも root-app は同期できる（kakeibon-apps は private なので、SecretStore を入れるまで同期できない）

---

## リポジトリの認証情報を ESO 管理に移す（1 回だけ・完了: 開発 2026-11-16、本番 2026-11-23）

> 完了済み。下の手順は記録として残す。本番は prod-cluster.md の段階 B（#21）で `kappaas-apps-secret` が同期され、古い `kappaas-apps-repo` を削除した。

以前は `bootstrap/argocd-repo-secret.yaml`（Secret `kappaas-apps-repo`）を kubectl apply していた。
これを ExternalSecret `kappaas-apps-secret`（`argocd-config/repositories/base/kappaas-apps.yaml`）に置き換える。

1. **[Mac]** PAT を Secrets Manager に登録する（値は今のクラスタの Secret から取る。画面に出さない）

   ```bash
   aws secretsmanager create-secret --region ap-northeast-1 \
     --name kappaas-apps/repositories/kappaas-apps/github-personal-access-token \
     --secret-string "$(kubectl --context prod -n argocd get secret kappaas-apps-repo -o jsonpath='{.data.password}' | base64 -d)"
   ```

   - 読み取り権限: 本番（KppaasRole、サイドカー化後は KappaasExternalSecretsRole）は `kappaas-apps/*`、開発の KappaasDevExternalSecretsRole は `kappaas-apps/repositories/*`（aws/rolesanywhere.yaml の既定）なので追加は不要
2. dev → main → release とマージし、各クラスタで `argocd-repositories` が同期されたら確認

   ```bash
   kubectl --context prod -n argocd get externalsecret kappaas-apps-secret     # SecretSynced / True
   kubectl --context prod -n argocd get secret kappaas-apps-secret -o jsonpath='{.metadata.labels}{"\n"}'   # secret-type: repository
   ```

3. **[Mac]** 古い Secret を削除し、Argo CD の Settings → Repositories で kappaas-apps が Successful のままであることを確認

   ```bash
   kubectl --context prod -n argocd delete secret kappaas-apps-repo
   kubectl --context dev -n argocd delete secret kappaas-apps-repo --ignore-not-found
   ```

4. Mac の `bootstrap/argocd-repo-secret.yaml`（gitignore 済み）を削除する（PAT が平文で残っているため）

- [x] 移行（開発・本番とも `kappaas-apps-secret` が SecretSynced、`kappaas-apps-repo` は削除済み）
- [ ] Mac の `bootstrap/argocd-repo-secret.yaml` の削除（手元のファイルなので各自で確認）
