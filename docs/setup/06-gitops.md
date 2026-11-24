# 6. Argo CD と AWS の認証（GitOps を始める）

本番・開発とも同じ手順。違うところは下の表のとおりに読み替える。[03-external.md](03-external.md) と、そのクラスタの 04 / 05 を済ませてから行う。

| | 本番 | 開発 |
|---|---|---|
| kubectl / helm のコンテキスト | `prod` | `dev` |
| checkout するブランチ | `release` | `dev` |
| dex の Secret | `bootstrap/argocd-dex-secret.yaml` | `bootstrap/argocd-dex-secret-dev.yaml` |
| Argo CD の values | `prod/values.yaml` | `dev/values.yaml` |
| HTTPProxy | `bootstrap/argo-server.yaml`（argocd.local） | `bootstrap/argo-server-dev.yaml`（argocd-dev.local） |
| root Application | `bootstrap/root-app-prod.yaml` → `applications/overlays/prod` | `bootstrap/root-app-dev.yaml` → `applications/overlays/dev` |
| rolesanywhere スタック | `kappaas-prod-rolesanywhere`（`Environment=prod`） | `kappaas-dev-rolesanywhere`（`Environment=dev`） |
| CA 証明書の保存先 | `~/k8s-backup/prod-ca.crt` | `~/k8s-backup/dev-ca.crt` |

以下の例は開発（`dev`）。

## 1. Mac の /etc/hosts

```
192.168.3.120 argocd.local kakeibon.local grafana.local
192.168.3.121 argocd-dev.local kakeibon-dev.local grafana-dev.local
```

## 2. Argo CD を helm で入れる

最初の 1 回だけ helm で入れる。以後は root-app が作る `argocd` アプリ（`charts/argocd`）で Argo CD 自身を管理する（[operations/argocd.md](../operations/argocd.md)）。

```bash
# [Mac] リポジトリのルートで、dev ブランチ（本番は release）を checkout した状態
kubectl --context dev create namespace argocd
kubectl --context dev apply -f bootstrap/argocd-dex-secret-dev.yaml      # Secret argocd-secret（dex の clientSecret・server.secretkey）
cd charts/argocd && helm dependency build \
  && helm --kube-context dev install argocd . -n argocd -f values.yaml -f dev/values.yaml \
  && rm -rf charts Chart.lock && cd -
kubectl --context dev -n argocd rollout status deploy/argocd-server
kubectl --context dev apply -f bootstrap/argo-server-dev.yaml            # HTTPProxy（TLS パススルー）
```

- リリース名は `argocd`（`applications/base/argocd.yaml` の `releaseName` と同じにする。違うとリソース名が変わる）
- `argocd-secret` はチャートで作らない（`configs.secret.createSecret: false`）。上で入れたものをそのまま使い続ける

## 3. root-app

```bash
# [Mac]
kubectl --context dev apply -f bootstrap/root-app-dev.yaml
kubectl --context dev -n argocd get app                                  # アプリが増えていく
```

root-app が `applications/overlays/dev` を同期し、次のものが入る。

| Application | 内容 |
|---|---|
| cert-manager | cert-manager と、クラスタの CA（Certificate `k8s-ca`、ClusterIssuer `k8s-ca`） |
| external-secrets | ESO（コントローラーに AWS 認証のサイドカー） |
| argocd-repositories | リポジトリの認証情報（ExternalSecret） |
| argocd-applications | `app-kakeibon`（`services/kakeibon/overlays/<env>`） |
| argocd | Argo CD 自身（**手動 Sync**。下の 6） |
| metrics-server・csi-driver-smb | |
| openebs-localpv | StorageClass `openebs-hostpath`（ノードのローカルディスク `/var/openebs/local` に PV を作る。PV はそのノードに固定。Prometheus・Grafana 用） |
| kube-prometheus-stack | Prometheus・Grafana（namespace `monitoring`。[operations/monitoring.md](../operations/monitoring.md)）。Grafana の秘密情報は Secrets Manager（[03-external.md](03-external.md)） |
| cloudflare-tunnel | 本番のみ |

- このリポジトリは public なので、認証情報が無くても同期できる
- この時点では AWS の認証ができないので、ExternalSecret（リポジトリの PAT・GHCR の PAT・JWT・Cloudflare・Grafana）が作れず、kakeibon は起動しない。次の 4・5 で直る

## 4. AWS: IAM Roles Anywhere（rolesanywhere スタック）

cert-manager が作るクラスタの CA（k8s-ca）を IAM Roles Anywhere のトラストアンカーにする。テンプレートは [aws/rolesanywhere.yaml](../../aws/rolesanywhere.yaml)（仕組みは [operations/aws-iam.md](../operations/aws-iam.md)）。

```bash
# [Mac] リポジトリのルートで。IAM ロールを作れる権限の AWS プロファイルで実行
kubectl --context dev get certificate k8s-ca -n cert-manager            # READY True になるまで待つ
kubectl --context dev get secret k8s-ca-secret -n cert-manager \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > ~/k8s-backup/dev-ca.crt
openssl x509 -in ~/k8s-backup/dev-ca.crt -noout -subject -enddate        # CN = k8s-on-prem-ca

aws cloudformation deploy --region ap-northeast-1 \
  --stack-name kappaas-dev-rolesanywhere \
  --template-file aws/rolesanywhere.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=dev CaCertificate="$(cat ~/k8s-backup/dev-ca.crt)"

aws cloudformation describe-stacks --region ap-northeast-1 --stack-name kappaas-dev-rolesanywhere \
  --query 'Stacks[0].Outputs' --output table
#   TrustAnchorArn / ProfileArn / ExternalSecretsRoleArn / KakeibonBackendRoleArn
```

作られるもの: トラストアンカーとプロファイル（`kappaas-<env>`）、ロール 2 つ（本番 `KappaasExternalSecretsRole`・`KakeibonBackendRole`、開発 `KappaasDevExternalSecretsRole`・`KakeibonBackendDevRole`）。

### ARN をリポジトリに書く（スタックを初めて作ったときだけ）

Outputs の ARN を次の 2 つのファイルに書き、feature ブランチ → dev への PR にする（本番はさらに dev → main → release）。

| ファイル | 書く値 |
|---|---|
| `charts/external-secrets/<env>/values.yaml` | `aws.trustAnchorArn` / `aws.profileArn` / `aws.roleArn`（ExternalSecretsRoleArn） |
| `services/kakeibon/overlays/<env>/patches/backend/aws-config-patch.yaml` | `TRUST_ANCHOR_ARN` / `PROFILE_ARN` / `ROLE_ARN`（KakeibonBackendRoleArn） |

- **クラスタを作り直しただけ（スタックは残っている）なら、ARN は変わらない。** 新しい CA で同じ deploy を実行すると、トラストアンカーの証明書だけが置き換わる。リポジトリの変更は不要
- 今の ARN はこの 2 つのファイルに書いてある（本番: トラストアンカー `a0d8433e-…`、開発: `f02a3107-…`）

## 5. SecretStore（Argo CD のリポジトリの認証情報用）

```bash
# [Mac] external-secrets が入って CRD ができてから
kubectl --context dev get crd secretstores.external-secrets.io
kubectl --context dev apply -f bootstrap/argocd-secrets.yaml             # SecretStore argocd-secret-store
```

- これだけは Argo CD の管理外（root-app より前に必要になりうるため bootstrap に置いている）
- ほかの SecretStore（kakeibon・cloudflare-tunnel）はそれぞれのマニフェストに含まれている

```bash
# [Mac] 確認
kubectl --context dev -n external-secrets get certificate external-secrets-aws                     # READY True
kubectl --context dev -n external-secrets get pods -l app.kubernetes.io/name=external-secrets      # READY 2/2（サイドカー）
kubectl --context dev get secretstore,externalsecret -A                                             # Ready / SecretSynced
```

- SecretSynced にならないときは、ExternalSecret を作り直すか `kubectl annotate externalsecret <名前> force-sync=$(date +%s) --overwrite` で再取得させる

## 6. argocd アプリを手動 Sync

Argo CD の画面（https://argocd-dev.local）で `argocd` アプリの **APP DIFF** を確認してから **SYNC** する。
これでサーバー証明書（Certificate `argocd-server`、k8s-ca で発行）が入る。

## 7. Mac で k8s-ca を信頼する

ブラウザの「保護されていない接続」を消すため、Mac でそのクラスタの k8s-ca を信頼する（Chrome・Edge・Safari は macOS のキーチェーンを使う）。

```bash
# [Mac]（4 で取り出した CA）
sudo security add-trusted-cert -d -r trustRoot -p ssl -k /Library/Keychains/System.keychain ~/k8s-backup/dev-ca.crt
```

- `-p ssl` で、TLS のサーバー証明書の検証にだけ信頼する
- クラスタを作り直して CA が変わったら、キーチェーンアクセスで古い CA を削除してから入れ直す（両クラスタの CA は同じ CN `k8s-on-prem-ca` なので、有効期限などで見分ける）
- 注意点は [operations/argocd.md](../operations/argocd.md)「サーバー証明書」

## 8. 確認

```bash
# [Mac]
kubectl --context dev -n argocd get app                 # 全 Synced / Healthy
kubectl --context dev -n argocd get app -o custom-columns=NAME:.metadata.name,REV:.spec.source.targetRevision   # dev（本番は release）
kubectl --context dev -n kakeibon get pods              # backend 2/2（サイドカー）、client 1/1
kubectl --context dev get deploy,sts -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,REPLICAS:.spec.replicas | awk 'NR==1 || $3>=2'   # 開発のみ: 見出しだけ
```

- [ ] https://argocd-dev.local に GitHub でログインできる（鍵マーク）
- [ ] https://kakeibon-dev.local でログインできる（本番は http://kakeibon.local と https://kakeibon.kappa-as.com）
- [ ] Settings → Repositories で kappaas-apps が Successful

開けないときは [operations/troubleshooting.md](../operations/troubleshooting.md)。
