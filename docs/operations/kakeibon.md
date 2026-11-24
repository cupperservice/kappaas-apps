# 業務アプリ kakeibon（services/kakeibon）

kakeibon（家計簿アプリ）のマニフェストは `services/kakeibon/` にあり、Argo CD の `app-kakeibon` が同期する。
アプリのソースとイメージのビルドは kakeibon リポジトリ（GitHub Actions が GHCR に push するだけで、マニフェストは更新しない）。

## 構成

```
services/kakeibon/
├── base/                         両環境で共通
│   ├── kustomization.yaml
│   ├── external-secrets.yaml     SecretStore と ExternalSecret（GHCR の PAT、JWT の署名鍵）
│   ├── backend/                  ConfigMap・Deployment・Service・ServiceAccount、AWS 認証のサイドカー用（aws-certificate.yaml・aws-config.yaml）
│   └── client/                   ConfigMap・Deployment・HTTPProxy・Service
└── overlays/
    ├── prod/patches/             本番の値
    │   ├── backend/aws-config-patch.yaml   IAM Roles Anywhere の ARN
    │   ├── backend/configmap-patch.yaml    S3 バケット・DynamoDB テーブル
    │   ├── client/httpproxy-patch.yaml     LAN 内のホスト名
    │   └── external-secrets-patch.yaml     JWT の署名鍵の取得元
    └── dev/                      開発の値（上と同じものに加えて、replicas 1 の deployment-patch と TLS 証明書）
```

- `base/external-secrets.yaml` は `.gitignore` の `secrets.yaml` に掛からない名前にしている
- Application は `argocd-config/applications/base/app-kakeibon.yaml`（`app-kakeibon`）。path と targetRevision は `argocd-config/applications/overlays/<env>/patches/app-kakeibon-patch.yaml`

| 項目 | 本番 | 開発 |
|---|---|---|
| DynamoDB | `kakeibon-prod` | `kakeibon-dev` |
| S3 | `kakeibo-data-bucket`（`S3_USER_BUCKET_NAME` も設定されているが使っていない） | `kakeibo-data-bucket-dev` |
| JWT（Secrets Manager） | `kakeibon/jwt-secret` | `kakeibon-dev/jwt-secret` |
| IAM ロール（サイドカー） | `KakeibonBackendRole` | `KakeibonBackendDevRole` |
| LAN 内のホスト名 | `http://kakeibon.local`（192.168.3.120） | `https://kakeibon-dev.local`（192.168.3.121。HTTP は HTTPS にリダイレクト。証明書は k8s-ca で発行した `overlays/dev/kakeibon-client-tls.yaml`） |
| 外部公開 | Cloudflare Tunnel（https://kakeibon.kappa-as.com） | なし |
| 同期するもの | `release` の `overlays/prod` | `dev` の `overlays/dev` |

- kakeibon-backend は Pod 内のサイドカー（aws_signing_helper）で IAM ロールを引き受けて DynamoDB / S3 / SES を使う（[aws-iam.md](aws-iam.md)）
- backend の `/readyz` は DynamoDB への疎通を確認し、client の `/readyz` は backend の `/health` を呼ぶ。**AWS の認証が無いと両方 Ready にならない**
- 以前のマニフェストの置き場所だった kakeibon-apps リポジトリは、どちらのクラスタからも参照されない（変更しても反映されない）

## イメージを更新する

1. kakeibon リポジトリの main にマージ → GitHub Actions が GHCR に push（タグは `backend` / `client` ディレクトリのツリーハッシュ）
2. `services/kakeibon/base/{backend,client}/deployment.yaml` の `image:` のタグを書き換え、feature ブランチ → dev への PR
3. 開発（https://kakeibon-dev.local）で確認 → dev → main → release で本番に反映

## 環境ごとの値を変える

- テーブル・バケット・JWT・ホスト名は `overlays/<env>/patches/` を変える
- テーブル・バケットを変えるときは、`aws/rolesanywhere.yaml` の `KakeibonTableArn` / `KakeibonBucketArns` / `KakeibonObjectArns` も変えてスタックを更新する（権限が無いと Ready にならない）
