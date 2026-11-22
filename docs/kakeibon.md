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
- kakeibon-backend は、Pod 内のサイドカー（aws_signing_helper）で IAM ロールを引き受けて DynamoDB / S3 / SES を使う（[iam-roles-anywhere.md](iam-roles-anywhere.md) の段階 2）
  - サイドカー・Certificate は `base/backend`（`deployment.yaml`・`aws-certificate.yaml`・`aws-config.yaml`）。ARN は環境ごとに `overlays/<env>/patches/backend/aws-config-patch.yaml`（本番 `KakeibonBackendRole`、開発 `KakeibonBackendDevRole`）
  - 本番は 2026-11 まで共用の aws-credential-helper（KppaasRole）を使っていた（prod-cluster.md の段階 C で切り替え）
  - backend の `/readyz` は DynamoDB への疎通を確認し、client の `/readyz` は backend の `/health` を呼ぶ。AWS の認証が無いと両方 Ready にならない
- Argo CD の Application は `argocd-config/applications/base/kakeibon-apps.yaml`（`app-kakeibon`）。path と targetRevision は `argocd-config/applications/overlays/<env>/patches/kakeibon-apps-patch.yaml`

## 本番の切り替え（2026-11-23 完了）

本番も 2026-11-23 から `services/kakeibon/overlays/prod`（release ブランチ）を同期している（[prod-cluster.md](prod-cluster.md)「base / overlays 構成・release ブランチへの移行」の段階 B、#21）。
それまでは kakeibon-apps リポジトリの `main`（`components`）を同期していた。

- 切り替えの前後で本番のマニフェストは変わっていない（`services/kakeibon/overlays/prod` の描画結果が kakeibon-apps main（e5d4188）の `components` と完全に一致することを確認してから切り替えた）。kakeibon の Pod は作り直されなかった
- **kakeibon-apps リポジトリはもう本番・開発のどちらからも参照されない。** イメージタグなどの変更は `services/kakeibon` だけで行う（kakeibon-apps を変更しても反映されない）
- kakeibon-apps のリポジトリ認証情報（`argocd-config/repositories/base/kakeibon-apps.yaml`）は削除した（prod-cluster.md の段階 C-5）。kakeibon-apps リポジトリ自体は残す

## イメージを更新する

1. kakeibon リポジトリの main にマージ → GitHub Actions が GHCR に push（タグは `backend` / `client` ディレクトリのツリーハッシュ）
2. `services/kakeibon/base/{backend,client}/deployment.yaml` の `image:` のタグを書き換え、feature ブランチ → dev への PR
3. 開発（https://kakeibon-dev.local）で確認 → dev → main → release で本番に反映

## 開発環境へのアクセス

Mac の `/etc/hosts` に追加する:

```
192.168.3.121 kakeibon-dev.local
```
