# ドキュメント

## 一から構築する（番号順に行う）

| | 内容 |
|---|---|
| [setup/01-overview.md](setup/01-overview.md) | 全体像と前提: 構成・IP・バージョン・外部サービス・Mac に必要なもの |
| [setup/02-node.md](setup/02-node.md) | ノードの準備（4 台共通）: Ubuntu・固定 IP・SSH / sudo・swap・sysctl・containerd・kubeadm |
| [setup/03-external.md](setup/03-external.md) | 外部サービス: GitHub（OAuth App・PAT）・Cloudflare Tunnel・AWS（kakeibon のリソース・Secrets Manager） |
| [setup/04-prod-cluster.md](setup/04-prod-cluster.md) | 本番クラスタ: kube-vip・kubeadm init・CP の join・Cilium・MetalLB・Contour |
| [setup/05-dev-cluster.md](setup/05-dev-cluster.md) | 開発クラスタ: altair の kubeadm init・kubeconfig・Cilium・MetalLB・Contour |
| [setup/06-gitops.md](setup/06-gitops.md) | Argo CD と AWS の認証: helm install・root-app・IAM Roles Anywhere・SecretStore・Mac の設定 |

04 と 05 は独立しているので、どちらか片方だけ作り直すときはその番号と 06 を行う。

## 運用

| | 内容 |
|---|---|
| [operations/repository.md](operations/repository.md) | ブランチ（dev → main → release）・ディレクトリ構成・chart を足すとき |
| [operations/argocd.md](operations/argocd.md) | Argo CD の更新・戻し方・サーバー証明書・クラスタの CA |
| [operations/kakeibon.md](operations/kakeibon.md) | 業務アプリ kakeibon の構成・イメージの更新 |
| [operations/aws-iam.md](operations/aws-iam.md) | IAM Roles Anywhere の仕組み・シークレットの追加・スタックの更新・**残作業（KppaasRole の削除）** |
| [operations/monitoring.md](operations/monitoring.md) | Prometheus・Grafana の構成・ログイン・保持期間・データの置き場所（ノード固定）・ディスク逼迫 |
| [operations/nodes.md](operations/nodes.md) | ノードの再起動・altair の停止と起動・kubelet の設定・CP の故障・SMC による強制減速 |
| [operations/troubleshooting.md](operations/troubleshooting.md) | ブラウザから開けない・401・NotReady・Pod が Ready にならない・ExternalSecret |

## 記録（history）

移行や作業の記録。今の手順ではない（当時の状態のまま残している）。

| | 内容 |
|---|---|
| [history/2026-10-control-plane-ha.md](history/2026-10-control-plane-ha.md) | 単一 CP から CP 3 台 + kube-vip への HA 化 |
| [history/2026-10-containerd-unify.md](history/2026-10-containerd-unify.md) | containerd を Docker 版 containerd.io 2.4.2 に統一 |
| [history/2026-11-iam-roles-anywhere.md](history/2026-11-iam-roles-anywhere.md) | IAM Roles Anywhere のロール分割とサイドカー化の設計・移行 |
| [history/2026-11-kakeibon-dev-aws.md](history/2026-11-kakeibon-dev-aws.md) | 開発の kakeibon 用 AWS リソースの作成、本番テーブルの kakeibon-prod への移行 |
| [history/2026-11-prod-migration.md](history/2026-11-prod-migration.md) | 本番の release ブランチ・base / overlays への移行（段階 A〜C）、Argo CD の自己管理化、入れ替え運用の記録 |
| [history/2026-11-monitoring.md](history/2026-11-monitoring.md) | Prometheus・Grafana の導入（保存先などの決定・ログインの不具合・sirius の LAN ケーブル抜けで Grafana が止まった件） |
