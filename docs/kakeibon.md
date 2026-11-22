# 業務アプリ kakeibon のマニフェスト（services/kakeibon）

kakeibon（家計簿アプリ）のマニフェストは `services/kakeibon/` にある。以前は kakeibon-apps リポジトリの `components/` にあった（2026-11-21 に移動）。
アプリのソースとイメージのビルドは kakeibon リポジトリ（GitHub Actions で GHCR に push するだけで、マニフェストは更新しない）。

## 構成

```
services/kakeibon/
├── base/                         両環境で共通（kakeibon-apps の components/ から環境ごとの値を除いたもの）
│   ├── kustomization.yaml
│   ├── external-secrets.yaml     SecretStore と ExternalSecret（イメージ取得用の PAT、JWT の署名鍵）。kakeibon-apps では secrets.yaml（.gitignore の secrets.yaml に掛かるので改名）
│   ├── backend/                  ConfigMap・Deployment・Service・ServiceAccount
│   └── client/                   ConfigMap・Deployment・HTTPProxy・Service
└── overlays/
    ├── prod/patches/             本番の値
    └── dev/patches/              開発の値
        ├── backend/configmap-patch.yaml   S3 バケット・DynamoDB テーブル
        ├── client/httpproxy-patch.yaml    LAN 内のホスト名
        └── external-secrets-patch.yaml    JWT の署名鍵の取得元
```

| 項目 | 本番 | 開発 |
|---|---|---|
| DynamoDB | `kakeibon-prod` | `kakeibon-dev` |
| S3 | `kakeibo-data-bucket`（`S3_USER_BUCKET_NAME` も設定。使っていない） | `kakeibo-data-bucket-dev` |
| JWT（Secrets Manager） | `kakeibon/jwt-secret` | `kakeibon-dev/jwt-secret` |
| LAN 内のホスト名 | `http://kakeibon.local`（192.168.3.120） | `https://kakeibon-dev.local`（192.168.3.121。HTTP は HTTPS にリダイレクト。証明書は k8s-ca で発行した `overlays/dev/kakeibon-client-tls.yaml`） |
| 外部公開 | Cloudflare Tunnel（kakeibon.kappa-as.com） | なし |
| 同期するブランチ（app-kakeibon） | `release` の `overlays/prod` | `dev` の `overlays/dev` |

- 開発用の AWS リソース（テーブル・バケット・JWT・IAM）は [kakeibon-dev-aws.md](kakeibon-dev-aws.md)
- 開発の kakeibon-backend は、Pod 内のサイドカー（aws_signing_helper）で `KakeibonBackendDevRole` を引き受けて DynamoDB / S3 / SES を使う（[iam-roles-anywhere.md](iam-roles-anywhere.md) の段階 2）
  - サイドカー・Certificate・ARN の ConfigMap は今は `overlays/dev` だけにある（`aws-certificate.yaml`・`aws-config.yaml`・`patches/backend/deployment-patch.yaml`）。本番は共用 helper（KppaasRole）のまま。本番に入れるときに base へ移す
  - backend の `/readyz` は DynamoDB への疎通を確認し、client の `/readyz` は backend の `/health` を呼ぶ。AWS の認証が無いと両方 Ready にならない
- Argo CD の Application は `argocd-config/applications/base/kakeibon-apps.yaml`（`app-kakeibon`）。path と targetRevision は `argocd-config/applications/overlays/<env>/patches/kakeibon-apps-patch.yaml`

## 本番の切り替え（未実施）

本番が `services/kakeibon` を同期するのは、本番の root-app を `release` に切り替えたとき（[prod-cluster.md](prod-cluster.md)「base / overlays 構成・release ブランチへの移行」）。
**それまで本番は kakeibon-apps リポジトリの `main`（`components`）を同期し続ける**。

- 切り替えの前後で本番のマニフェストは変わらない（`services/kakeibon/overlays/prod` の描画結果が kakeibon-apps の `components` と同じことを 2026-11-21 に確認済み）。Argo CD はリソースを作り直さない
- 切り替えまでに本番を更新する場合（イメージタグなど）は、kakeibon-apps の `main` と `services/kakeibon/base` の両方を変更する（片方だけだと、切り替えたときに古いものに戻る）
- 切り替え後、kakeibon-apps のリポジトリ認証情報（`argocd-config/repositories/base/kakeibon-apps.yaml`）は不要になる。kakeibon-apps リポジトリ自体は残す

## イメージを更新する

1. kakeibon リポジトリの main にマージ → GitHub Actions が GHCR に push（タグは `backend` / `client` ディレクトリのツリーハッシュ）
2. `services/kakeibon/base/{backend,client}/deployment.yaml` の `image:` のタグを書き換え、feature ブランチ → dev への PR
3. 開発（https://kakeibon-dev.local）で確認 → dev → main → release で本番に反映

## 開発環境へのアクセス

Mac の `/etc/hosts` に追加する:

```
192.168.3.121 kakeibon-dev.local
```
