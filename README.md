# kappaas-apps

自宅の Mac mini 4 台で動かす Kubernetes クラスタ（本番・開発）の GitOps リポジトリ。Argo CD がこのリポジトリを同期する。

## クラスタ構成

| クラスタ | ノード | kubeconfig のコンテキスト | Argo CD | 同期するブランチ |
|---|---|---|---|---|
| 本番 | sirius / capella / spica（CP 3 台固定） | `prod` | https://argocd.local | `release` |
| 開発 | altair（1 台） | `dev` | https://argocd-dev.local | `dev` |

変更は feature ブランチ → `dev`（開発クラスタに反映して確認）→ `main`（統合。どこにもデプロイされない）→ `release`（本番に反映）の順に PR でマージする。
業務アプリ kakeibon のマニフェストも `services/kakeibon` にあり、同じ流れで反映する。

## ドキュメント

[docs/README.md](docs/README.md)（一から構築する手順・運用・作業の記録）
