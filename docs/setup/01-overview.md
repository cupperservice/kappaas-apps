# 1. 全体像と前提

この環境を一から作るときの前提をまとめる。手順は 02 から番号順に行う。

## 構成

```
                      ┌──────────────────────── 本番クラスタ（prod）────────────────────────┐
                      │            VIP 192.168.3.250:6443（kube-vip）                         │
                      │     sirius (.124)        capella (.126)        spica (.127)           │
                      │     CP + etcd            CP + etcd             CP + etcd               │
                      │     3 台ともワークロードも載せる（control-plane の taint なし）       │
                      └───────────────────────────────────────────────────────────────────────┘
                      ┌──── 開発クラスタ（dev）────┐
                      │  altair (.125)              │
                      │  CP 1 台・ワークロードも載せる │
                      └─────────────────────────────┘
```

| 項目 | 本番 | 開発 |
|---|---|---|
| ノード | sirius / capella / spica（CP 3 台固定） | altair（1 台） |
| API サーバー | VIP `192.168.3.250:6443`（kube-vip） | `192.168.3.125:6443`（kube-vip なし） |
| kubeadm の clusterName | `kubernetes`（既定） | `dev` |
| Mac の kubeconfig のコンテキスト | `prod` | `dev`（ユーザー名 `dev-admin`） |
| MetalLB の IP | `192.168.3.120` | `192.168.3.121` |
| Argo CD | https://argocd.local | https://argocd-dev.local |
| 同期するブランチ | `release` | `dev` |
| Grafana | https://grafana.local | https://grafana-dev.local |
| kakeibon | http://kakeibon.local、外部公開 https://kakeibon.kappa-as.com（Cloudflare Tunnel） | https://kakeibon-dev.local（LAN 内のみ） |
| AWS | IAM Roles Anywhere（本番の CA） | IAM Roles Anywhere（開発の CA） |

- 2 つのクラスタは完全に別（etcd・CA・Argo CD・AWS のトラストアンカーすべて別）。開発で壊しても本番に影響しない
- 本番の etcd は 3 メンバー。1 台の故障に耐える（2 台止まるとクラスタ全体が止まる）
- 開発は 1 台なので replicas は 1・PDB は作らない（[05-dev-cluster.md](05-dev-cluster.md)）

## ハードウェアとネットワーク

| ホスト名 | IP | 用途 |
|---|---|---|
| sirius | 192.168.3.124 | 本番 CP |
| altair | 192.168.3.125 | 開発 |
| capella | 192.168.3.126 | 本番 CP |
| spica | 192.168.3.127 | 本番 CP |
| （VIP） | 192.168.3.250 | 本番の API サーバー（kube-vip が ARP で広告） |
| （MetalLB） | 192.168.3.120 / .121 | 本番 / 開発の Contour（LoadBalancer） |
| （NAS） | 192.168.3.10 | SMB 共有 `k8s-disk-1`（guest）。`charts/csi-driver-smb` の PV |

- 本体はすべて Mac mini（Macmini7,1）。NIC は `enp3s0f0`（kube-vip の `--interface` に使う）
- ディスク: 本番 3 台は SSD（sirius・spica は NVMe、capella は SATA）。altair は OS が HDD で、PV・etcd・containerd は 128GB の SSD に置く（[operations/nodes.md](../operations/nodes.md)「altair のディスク」）
- OS は Ubuntu 26.04（Server）。IP は各ノードで固定する（[02-node.md](02-node.md)）。ルーターの DHCP の範囲から上の IP（.120〜.127、.250）を外しておく
- ノードの SMC（本体の管理チップ）が誤動作すると CPU が強制減速される（[operations/nodes.md](../operations/nodes.md)「SMC による CPU の強制減速」）

## バージョン（2026-11-23 時点）

| 対象 | バージョン |
|---|---|
| Ubuntu | 26.04（resolute） |
| Kubernetes（kubeadm / kubelet / kubectl） | v1.36.5 |
| containerd | containerd.io 2.4.2（Docker のリポジトリ） |
| kube-vip | v1.3.1（本番のみ） |
| Cilium | 1.19.4（Helm） |
| MetalLB | 0.15.3（Helm） |
| Contour | chart 0.5.0（app 1.33.4、リリース名 `my-release`） |
| Argo CD | argo-cd chart 9.5.14（`charts/argocd`） |
| そのほかの chart | `charts/*/Chart.yaml`（cert-manager・external-secrets・cloudflare-tunnel・csi-driver-smb・metrics-server・openebs-localpv・kube-prometheus-stack） |

Cilium・MetalLB・Contour は Argo CD の管理外（クラスタ構築時に helm で入れる）。それ以外は Argo CD が `charts/` から入れる。

## 外部サービス

| サービス | 使うもの | 手順 |
|---|---|---|
| GitHub | このリポジトリ（public）、Argo CD のログイン（OAuth App）、kakeibon のイメージ（GHCR） | [03-external.md](03-external.md) |
| Cloudflare | Tunnel `k8s-on-prem`（kakeibon.kappa-as.com を本番に転送） | [03-external.md](03-external.md) |
| AWS（ap-northeast-1） | Secrets Manager、IAM Roles Anywhere、kakeibon の DynamoDB・S3・SES | [03-external.md](03-external.md)、[06-gitops.md](06-gitops.md) |

## Mac（作業端末）に必要なもの

- `kubectl`、`helm`、`aws`（IAM ロールを作れる権限のプロファイル）、`git`、`gh`、`openssl`、`cloudflared`
- 4 台に SSH 鍵で入れること（`~/.ssh/config` にホスト名ごとの定義。[02-node.md](02-node.md)）
- このリポジトリと kakeibon リポジトリ（CloudFormation のテンプレート）の clone
- 秘密情報の置き場所 `~/k8s-backup/`（CA 証明書・バックアップ）。リポジトリの外に置く

## 手順の表記

- **[Mac]** = kubectl / helm / aws を使う端末
- **[node]** = 対象のノードに SSH（`ssh <ホスト名>`）
- **[sirius]** など = そのノードに SSH
- **[GitHub] [AWS] [Cloudflare]** = それぞれの画面または CLI
