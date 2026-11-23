# 本番の base / overlays・release ブランチへの移行（2026-11）

> **記録**（2026-11 の作業）。現在の構築・運用の手順は [docs/README.md](../README.md)。
> 元は prod-cluster.md・argocd-self-managed.md・kakeibon.md にあった記録をまとめたもの。文中のファイル名・リンクは当時のもの。

## base / overlays 構成・release ブランチへの移行（段階 A・B は 2026-11-23 完了、段階 C は未実施）

本番の root-app を `main` / `applications/base`（旧構成）から `release` / `applications/overlays/prod` に切り替え、
dev に入っていた変更（#8〜#18）を本番に入れた。一度に入れず、次の 3 段階に分けた。

| 段階 | 内容 | 状態 |
|---|---|---|
| A | root-app を release に切り替える（構成だけ。中身は変えない） | 2026-11-23 完了 |
| B | 本番の rolesanywhere スタックを作り、dev → main → release で本番に反映 | 2026-11-23 完了 |
| C | kakeibon-backend のサイドカーを本番に入れ、共用 aws-credential-helper・KppaasRole を廃止 | 未実施 |

**順番の理由**: 段階 A を最初に行う。旧構成の root-app（main / applications/base）が動いている間に新構成を main に入れると、
root-app が環境ごとの値の無い base を同期し、aws-credential-helper（ARN が消える）と cloudflare-tunnel（トンネル設定が消える）が
自動同期で壊れて kakeibon が止まる。段階 A で本番の参照先を release に移してから main を更新する。

### 段階 A: root-app を release に切り替える（2026-11-23 実施）

切り替え時の release は fcbccc1（新構成の骨組みだけ）。中身は今の本番と同じなので、Pod は作り直されない。

1. **[Mac]** 事前確認: 3 台 Ready、etcd の Pod 3 つ Running、Running / Completed 以外の Pod が無い
2. **[Mac]** 差分の確認（読み取りのみ）
   - 本番の各 Application の spec と、release の `applications/overlays/prod` の描画を比較 → 変わるのは targetRevision（main → release）・path・helm.valueFiles だけ
   - 各 chart の `values.yaml` + `prod/values.yaml` を重ねた結果が、main の `values.yaml` と一致。templates・Chart.yaml・argocd-config の base も一致
   - `kubectl --context prod diff -f bootstrap/root-app-prod.yaml` → path と targetRevision の 2 行だけ
3. **[Mac]** 切り替え

   ```bash
   kubectl --context prod apply -f bootstrap/root-app-prod.yaml
   ```

4. **[Mac]** 確認: root-app が `release` / `applications/overlays/prod`、全アプリ Synced / Healthy、app-kakeibon は引き続き kakeibon-apps の main
   - この時点では `argocd-config/applications/overlays/prod/patches/kakeibon-apps-patch.yaml`（#9）は release に無いので効かない。正常

### 段階 B: dev の変更を本番に入れる（2026-11-23 実施）

1. **[AWS]** 本番の rolesanywhere スタックを作る（`aws/rolesanywhere.yaml`、`Environment=prod`、本番の CA）

   ```bash
   kubectl --context prod get secret k8s-ca-secret -n cert-manager -o jsonpath='{.data.tls\.crt}' | base64 -d > ~/k8s-backup/prod-ca.crt
   aws cloudformation deploy --region ap-northeast-1 --stack-name kappaas-prod-rolesanywhere \
     --template-file aws/rolesanywhere.yaml --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides Environment=prod CaCertificate="$(cat ~/k8s-backup/prod-ca.crt)"
   ```

   - 作られたもの: トラストアンカー `a0d8433e-…`・プロファイル `76d3d14c-…`（`kappaas-prod`）、`KappaasExternalSecretsRole`、`KakeibonBackendRole`（段階 C で使う）
   - 既存の KppaasRole・旧トラストアンカー・プロファイルには触れない
2. Outputs の ARN を `charts/external-secrets/prod/values.yaml` に書く（#18）
   - **このとき不具合が見つかった**: #8 でコミットした `charts/external-secrets/templates/aws-config.yaml` に開発の ARN が直接書かれていた。
     そのまま本番に入れると、本番の external-secrets が開発のロールを使って認証に失敗していた。values（`aws.*`）から読む形に直した（#18）
3. 本番に入る差分の確認（release と dev の本番向けの描画を比較）

   | 対象 | 差分 |
   |---|---|
   | applications/overlays/prod | なし |
   | app-kakeibon | 取得元が kakeibon-apps → `services/kakeibon/overlays/prod`。描画は kakeibon-apps main（e5d4188）と完全に一致 |
   | argocd-repositories | ExternalSecret `kappaas-apps-secret` が増える |
   | argocd | Certificate `argocd-server`（argocd.local） |
   | external-secrets | サイドカー・Certificate・ConfigMap（本番の ARN） |
   | それ以外の chart | 変化なし |

   - 本番クラスタへのサーバー側 dry-run で、増える・変わるリソースがすべて受け付けられることを確認
4. dev → main（#19）、main → release（#21）をマージ → 本番が同期
5. 確認

   ```bash
   kubectl --context prod -n argocd get app                                   # 全 Synced / Healthy
   kubectl --context prod -n external-secrets get pods -l app.kubernetes.io/name=external-secrets   # 2/2（サイドカー）
   kubectl --context prod get secretstore,externalsecret -A                    # Ready / SecretSynced
   ```

   - external-secrets が 2/2、`kappaas-apps-secret` が新しいロール（KappaasExternalSecretsRole）で同期できた。ExternalSecret 5 つすべて SecretSynced
   - kakeibon の Pod は作り直されず、https://kakeibon.kappa-as.com は 200、ログインできる
6. `argocd` アプリを手動 Sync（Certificate `argocd-server`）→ Mac で本番の k8s-ca を信頼（[argocd-self-managed.md](../operations/argocd.md)「サーバー証明書」）→ https://argocd.local が鍵マーク
7. 古い Secret `kappaas-apps-repo`（手で入れていたリポジトリの認証情報）を削除

戻し方（段階 B の後に問題が出た場合）: main → release のマージを revert する PR を release に入れると、本番は fcbccc1 の状態に戻る。
external-secrets で問題が出ても、作成済みの Secret は残るので kakeibon はすぐには止まらない。

### 段階 C: kakeibon-backend のサイドカーと共用 helper の廃止（C-1〜C-3・C-5 は 2026-11-23、C-4 は未実施）

[iam-roles-anywhere.md](2026-11-iam-roles-anywhere.md) の段階 2 の本番分とステップ 4・5。

1. **C-1（#23）**: `services/kakeibon/overlays/dev` にあったサイドカー一式を base に移し、ARN だけを環境ごとにした（`overlays/<env>/patches/backend/aws-config-patch.yaml`。本番は `KakeibonBackendRole`）
2. **C-2（#24・#25）**: dev → main → release で本番に反映。backend がサイドカー付きの新しい Pod（2/2）に 1 つずつ入れ替わり、`/readyz`（DynamoDB）が 200、ブラウザでログイン・データ・ファイル・メールが動くことを確認
3. **C-3**: 共用の aws-credential-helper を廃止する（`applications/base/aws-credential-helper.yaml`・`charts/aws-credential-helper/`・Dependabot の docker の設定を削除）
   - Application に finalizer があるので、Argo CD が DaemonSet・Service・Certificate を削除する。Namespace `aws-credential-helper` は残るので、空になったら手で削除する
   - 確認: 各ノードの 9911 番ポートが LAN から応答しない（下のコマンド）
4. **C-4（未実施）**: 1 週間ほど様子を見て、KppaasRole と旧トラストアンカー・プロファイルを削除する（iam-roles-anywhere.md のステップ 5。CloudTrail で KppaasRole が使われていないことを確認してから）
5. **C-5**: kakeibon-apps リポジトリの認証情報（`argocd-config/repositories/base/kakeibon-apps.yaml`）を削除する
   - ExternalSecret の creationPolicy が Owner なので、Secret `kakeibon-apps-secret` も一緒に消える
   - Secrets Manager の `kappaas-apps/repositories/kakeibon-apps/github-personal-access-token` も不要になる（削除は手で。GitHub の PAT も失効させる）

```bash
# [Mac] C-3 の確認
kubectl --context prod get ns aws-credential-helper            # 中身が空になったら kubectl delete ns aws-credential-helper
kubectl --context prod -n aws-credential-helper get all
for ip in 192.168.3.124 192.168.3.126 192.168.3.127; do
  curl -s -o /dev/null -w "$ip: %{http_code}\n" --max-time 3 -X PUT http://$ip:9911/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'                # 000（応答なし）になること
done
```

- [x] 段階 A
- [x] 段階 B
- [x] 段階 C の C-1〜C-3・C-5
- [ ] 段階 C の C-4（KppaasRole と旧トラストアンカー・プロファイルの削除）→ 手順は [operations/aws-iam.md](../operations/aws-iam.md)


---

## kakeibon の本番の切り替え（kakeibon.md から）

本番も 2026-11-23 から `services/kakeibon/overlays/prod`（release ブランチ）を同期している（上の段階 B、#21）。
それまでは kakeibon-apps リポジトリの `main`（`components`）を同期していた。

- 切り替えの前後で本番のマニフェストは変わっていない（`services/kakeibon/overlays/prod` の描画結果が kakeibon-apps main（e5d4188）の `components` と完全に一致することを確認してから切り替えた）。kakeibon の Pod は作り直されなかった
- **kakeibon-apps リポジトリはもう本番・開発のどちらからも参照されない。** イメージタグなどの変更は `services/kakeibon` だけで行う（kakeibon-apps を変更しても反映されない）
- kakeibon-apps のリポジトリ認証情報（`argocd-config/repositories/base/kakeibon-apps.yaml`）は削除した（prod-cluster.md の段階 C-5）。kakeibon-apps リポジトリ自体は残す

---

## Argo CD の初回の取り込み（argocd-self-managed.md から。2026-11-03）

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
- [x] Mac の `bootstrap/argocd-repo-secret.yaml` の削除（2026-11-23）

---

## 暴走の原因調査（2026-11-02 の件）

暴走したノードで実行する（`-b -1` は前回の起動時のログ）。

```bash
sudo journalctl -b -1 -p warning --no-pager | tail -100
sudo journalctl -k -b -1 --no-pager | grep -i -E 'oom|out of memory|hung task|soft lockup|watchdog|I/O error'
sudo journalctl -b -1 -u kubelet --no-pager | grep -i -E 'evict|pressure' | tail -30
```

- [ ] 原因: （記入）。2026-11-17 の capella と同じ症状だった可能性がある（[operations/nodes.md](../operations/nodes.md)「SMC による CPU の強制減速」）


---

## 稼働 3 台 + 待機 1 台の入れ替え運用（2026-11-03〜11-15、廃止）

altair を開発用にするため、2026-11-15 に廃止した（scripts/rotate-cp.sh は削除）。

| 日付 | 入った（in） | 休んだ（out） | 備考 |
| ---- | ------------ | ------------- | ---- |
| 2026-11-03 | capella | altair | |
