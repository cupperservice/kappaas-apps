# 3. 外部サービスの準備（GitHub・Cloudflare・AWS）

クラスタに依存しないものを先に用意する。クラスタの CA が必要な IAM Roles Anywhere は [06-gitops.md](06-gitops.md) で作る。

**秘密情報（PAT・Client secret・credentials）は画面に出さず、Git に入れない。** Secrets Manager に入れるか、gitignore 済みのファイル（`bootstrap/argocd-dex-secret*.yaml`）に書く。

## GitHub

### リポジトリとブランチ

- kappaas-apps（このリポジトリ、public）。ブランチ `dev`・`main`・`release`（[operations/repository.md](../operations/repository.md)）
- kakeibon（アプリのソース。GitHub Actions が GHCR にイメージを push する）

### OAuth App（Argo CD の GitHub ログイン）

コールバック URL は 1 つしか登録できないので、本番用と開発用を別に作る（Settings → Developer settings → OAuth Apps）。

| | 本番 | 開発 |
|---|---|---|
| Homepage URL | `https://argocd.local` | `https://argocd-dev.local` |
| Authorization callback URL | `https://argocd.local/api/dex/callback` | `https://argocd-dev.local/api/dex/callback` |
| Client ID を書く場所 | `charts/argocd/prod/values.yaml` の `clientID` | `charts/argocd/dev/values.yaml` の `clientID` |
| Client secret を書く場所 | `bootstrap/argocd-dex-secret.yaml` | `bootstrap/argocd-dex-secret-dev.yaml` |

`bootstrap/argocd-dex-secret*.yaml` は gitignore 済み（Mac にだけ置く）。形式:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: argocd-secret
  namespace: argocd
  labels:
    app.kubernetes.io/name: argocd-secret
    app.kubernetes.io/part-of: argocd
type: Opaque
stringData:
  dex.github.clientSecret: <OAuth App の Client secret>
  server.secretkey: <openssl rand -base64 32 の出力。環境ごとに別の値>
```

- Argo CD にログインできるユーザー・権限は `charts/argocd/values.yaml` の `rbac`（`policy.csv`）

### OAuth App（Grafana の GitHub ログイン）

Argo CD とは別に、本番用と開発用を作る。

| | 本番 | 開発 |
|---|---|---|
| Homepage URL | `https://grafana.local` | `https://grafana-dev.local` |
| Authorization callback URL | `https://grafana.local/login/github` | `https://grafana-dev.local/login/github` |
| Client ID を書く場所 | `charts/kube-prometheus-stack/prod/values.yaml` の `client_id` | `charts/kube-prometheus-stack/dev/values.yaml` の `client_id` |
| Client secret を入れる場所 | Secrets Manager `kappaas-apps/monitoring/grafana`（下の表） | Secrets Manager `kappaas-apps/monitoring-dev/grafana` |

- ログインできるのは GitHub アカウント `cupperservice` だけ（`charts/kube-prometheus-stack/values.yaml` の `role_attribute_path`）

### PAT（Personal Access Token）

| 用途 | 権限 | Secrets Manager の名前 |
|---|---|---|
| kakeibon のイメージを GHCR から取る | `read:packages` | `kappaas-apps/ghcr/github-personal-access-token` |
| Argo CD がこのリポジトリを読む | このリポジトリの Contents の読み取り | `kappaas-apps/repositories/kappaas-apps/github-personal-access-token` |

- このリポジトリは public なので、2 つ目が無くても Argo CD は同期できる（レート制限の対策として入れている）

## Cloudflare Tunnel（本番のみ）

kakeibon.kappa-as.com を本番の kakeibon に転送する。cloudflared は本番クラスタの中で動く（`charts/cloudflare-tunnel`）。

**[Mac]**

```bash
cloudflared tunnel login                              # ブラウザで kappa-as.com のゾーンを選ぶ
cloudflared tunnel create k8s-on-prem                 # ~/.cloudflared/<トンネル ID>.json ができる
cloudflared tunnel route dns k8s-on-prem kakeibon.kappa-as.com
```

- `charts/cloudflare-tunnel/prod/values.yaml` の `account`・`tunnelId`・`ingress` を合わせる
- credentials（`<トンネル ID>.json`）を Secrets Manager の `kappaas-apps/cloudflare/credentials` に入れる（下の表）。ExternalSecret が Secret `cloudflare-tunnel-credentials` の `credentials.json` にする
- トンネルのトークンが必要なとき（今の構成では使わない）: `cloudflared tunnel token --cred-file ./credentials.json k8s-on-prem`（以前の README には `k0s-on-prem` とあったが、トンネル名は `k8s-on-prem`）

## AWS（ap-northeast-1）

### kakeibon のリソース

テンプレートは kakeibon リポジトリの `cloudformation/`。**[Mac]** kakeibon リポジトリのルートで実行する。

| リソース | 本番 | 開発 |
|---|---|---|
| DynamoDB（`cloudformation/DynamoDB.yaml`） | `kakeibon-prod`（スタック `kakeibon-dynamodb-prod`、`Environment=prod`） | `kakeibon-dev`（スタック `kakeibon-dynamodb`） |
| S3（`cloudformation/S3.yaml`） | `kakeibo-data-bucket`（**手作業で作ったもの。スタックなし**） | `kakeibo-data-bucket-dev`（スタック `kakeibon-s3-dev`、`Environment=dev`） |
| SES | スタック `kakeibon-ses`（ドメイン `kawashima-kazuh.com`、送信元 `noreply@kawashima-kazuh.com`）。両環境で共用 | ← |

```bash
aws cloudformation deploy --region ap-northeast-1 --stack-name kakeibon-dynamodb-prod \
  --template-file cloudformation/DynamoDB.yaml --parameter-overrides Environment=prod
aws cloudformation deploy --region ap-northeast-1 --stack-name kakeibon-s3-dev \
  --template-file cloudformation/S3.yaml --parameter-overrides Environment=dev
```

- 本番のバケットを作り直す場合、名前が変わるなら `aws/rolesanywhere.yaml`（`KakeibonBucketArns`）と `services/kakeibon/overlays/prod/patches/backend/configmap-patch.yaml` も変える
- S3 のバケット名は全世界で一意。作れなかったときは名前を変え、上の 2 つのファイルも合わせる
- 開発の `kakeibon-dev` テーブルには、本番を `kakeibon-prod` に移したときのデータの写しが入っている（[history/2026-11-kakeibon-dev-aws.md](../history/2026-11-kakeibon-dev-aws.md)）

### Secrets Manager

external-secrets（ESO）がクラスタに取り込む。値は画面に出さずに登録する（`--secret-string "$(...)"`、またはファイルから `file://`）。

| 名前 | 中身 | 使う場所 |
|---|---|---|
| `kappaas-apps/repositories/kappaas-apps/github-personal-access-token` | PAT の文字列 | `argocd-config/repositories/base/kappaas-apps.yaml`（両環境） |
| `kappaas-apps/ghcr/github-personal-access-token` | Docker の認証 JSON（`{"auths":{"ghcr.io":{"auth":"<base64(ユーザー名:PAT)>"}}}`） | `services/kakeibon/base/external-secrets.yaml`（両環境） |
| `kappaas-apps/cloudflare/credentials` | トンネルの credentials JSON | `charts/cloudflare-tunnel`（本番のみ） |
| `kakeibon/jwt-secret` | JWT の署名鍵（文字列） | `services/kakeibon/overlays/prod`（本番） |
| `kakeibon-dev/jwt-secret` | JWT の署名鍵（文字列） | `services/kakeibon/overlays/dev`（開発） |
| `kappaas-apps/monitoring/grafana` | JSON `{"github-client-secret":"<OAuth App の Client secret>","admin-password":"<ランダム>"}` | `charts/kube-prometheus-stack`（本番） |
| `kappaas-apps/monitoring-dev/grafana` | 同上（開発の OAuth App・別のパスワード） | `charts/kube-prometheus-stack`（開発） |
| `kappaas-apps/cloudflare/token` | トンネルのトークン | **どこからも参照されていない**（以前の cloudflared の起動方法の名残と思われる） |

```bash
# [Mac] 例: JWT（ランダムな値を作って直接入れる）
aws secretsmanager create-secret --region ap-northeast-1 --name kakeibon-dev/jwt-secret \
  --tags Key=Environment,Value=dev Key=Project,Value=kakeibon \
  --secret-string "$(openssl rand -base64 48)"

# 例: GHCR の認証 JSON（PAT を read -s で読み、画面に出さない）
read -s PAT
aws secretsmanager create-secret --region ap-northeast-1 --name kappaas-apps/ghcr/github-personal-access-token \
  --secret-string "{\"auths\":{\"ghcr.io\":{\"auth\":\"$(printf '%s' "cupperservice:$PAT" | base64)\"}}}"
unset PAT

# 例: Grafana（Client secret を read -s で読み、admin のパスワードはランダムに作る）
read -s CS
aws secretsmanager create-secret --region ap-northeast-1 --name kappaas-apps/monitoring-dev/grafana \
  --secret-string "{\"github-client-secret\":\"$CS\",\"admin-password\":\"$(openssl rand -base64 24)\"}"
unset CS

# 例: Alertmanager の通知先（Slack の Incoming Webhook の URL を read -s で読む。本番は kappaas-apps/monitoring/alertmanager）
read -s URL
aws secretsmanager create-secret --region ap-northeast-1 --name kappaas-apps/monitoring-dev/alertmanager \
  --secret-string "{\"slack-webhook-url\":\"$URL\"}"
unset URL

# 例: Cloudflare の credentials
aws secretsmanager create-secret --region ap-northeast-1 --name kappaas-apps/cloudflare/credentials \
  --secret-string file://$HOME/.cloudflared/<トンネル ID>.json
```

- どのロールがどのシークレットを読めるかは `aws/rolesanywhere.yaml` の `SecretArns`（本番は `kappaas-apps/*`・`kakeibon/*`、開発は `kappaas-apps/repositories/*`・`kappaas-apps/ghcr/*`・`kakeibon-dev/*`）
- シークレットを追加するときは [operations/aws-iam.md](../operations/aws-iam.md)「シークレットを追加する」
