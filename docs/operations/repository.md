# リポジトリの運用（ブランチ・ディレクトリ構成）

## ブランチ

```
feature ブランチ → dev（開発クラスタが同期して確認）→ main（統合。どこにもデプロイされない）→ release（本番が同期）
```

- **dev に直接コミットしない。** feature ブランチを作り、dev への PR にする
- dev → main、main → release も PR でマージする。**release へのマージが本番へのリリース**
- インフラの変更（chart のバージョン上げなど）も業務アプリ（kakeibon）の変更も同じ流れ
- Dependabot は `charts/*/Chart.yaml` の依存チャートを見て、dev ブランチに PR を作る（`.github/dependabot.yml` の `target-branch: dev`）

## ディレクトリ構成

```
applications/                 root-app が同期する Application（Argo CD の管理対象の一覧）
├── base/                     両環境で共通の Application（valueFiles: [values.yaml]）
└── overlays/{prod,dev}/
    ├── kustomization.yaml    resources: ../../base + patches
    └── patches/<base のファイル名>-patch.yaml   ブランチ・環境ごとの values を足す

argocd-config/
├── applications/             業務アプリの Application（app-kakeibon）
└── repositories/             リポジトリの認証情報（ExternalSecret）
    （それぞれ base/ と overlays/{prod,dev}/）

charts/<chart>/               Helm チャートのラッパー
├── Chart.yaml                依存チャートとバージョン（Dependabot が更新）
├── values.yaml               両環境で共通の値
├── prod/values.yaml          本番の値
├── dev/values.yaml           開発の値（本番にしか入れない cloudflare-tunnel には無い）
└── templates/                追加のリソース（CA、SecretStore など）

services/kakeibon/            業務アプリ kakeibon（operations/kakeibon.md）
├── base/
└── overlays/{prod,dev}/patches/

bootstrap/                    クラスタ構築時に kubectl apply するもの（setup/06-gitops.md）
aws/rolesanywhere.yaml        IAM Roles Anywhere の CloudFormation テンプレート（operations/aws-iam.md）
tools/                        作業用のスクリプト（DynamoDB のコピーなど）
```

### overlays・patches の決まり

- patch は base のファイルごとに 1 つ、`overlays/<env>/patches/<base のファイル名>-patch.yaml` に置く
- patch は strategic merge 形式（Application のマニフェストの一部を書く）。`metadata.name` で対象が決まるので kustomization.yaml に target は書かない
- **リスト（`valueFiles`、`sources`、ExternalSecret の `data` など）は丸ごと置き換わる**ので、patch には全要素を書く（例: `valueFiles: [values.yaml, dev/values.yaml]`）
- Application は base で `values.yaml` を指定し、patch で `prod/values.yaml` / `dev/values.yaml` を後ろに足す（後ろが優先）
- 開発に入れないもの（cloudflare-tunnel）は dev の patch で `$patch: delete`

### 新しい chart（Application）を足すとき

1. `charts/<chart>/`（Chart.yaml・values.yaml・prod/values.yaml・dev/values.yaml）を作る
2. `applications/base/<chart>.yaml` を作り、`applications/base/kustomization.yaml` に足す
3. `applications/overlays/{prod,dev}/patches/<chart>-patch.yaml`（targetRevision とその環境の values）を作り、両方の kustomization.yaml に足す
4. **開発の values で replicas を 1、PDB を作らない**（[setup/05-dev-cluster.md](../setup/05-dev-cluster.md)「開発クラスタの決まり」）
5. `.github/dependabot.yml` の `directories` に `/charts/<chart>` を足す（ディレクトリを 1 つずつ並べているので、足さないと更新の PR が来ない）

## 変更を入れるときの確認

```bash
# [Mac] 描画結果を確認（例: 開発の Application 一覧）
kubectl kustomize applications/overlays/dev
kubectl kustomize services/kakeibon/overlays/prod
# chart の描画
cd charts/<chart> && helm dependency build && helm template x . -f values.yaml -f prod/values.yaml | less; rm -rf charts Chart.lock; cd -
```

- コミットの前に `git diff --cached` で、ステージした内容が確認したものと同じかを見る（確認の後にファイルが変わっていることがある）
- 本番に入る差分は、release と main（または dev）の描画を比べて確認する
