# メトリクス（Prometheus・Grafana）

本番・開発それぞれのクラスタに kube-prometheus-stack を 1 式ずつ入れている（クラスタ間の依存は作らない）。

| | 本番 | 開発 |
|---|---|---|
| Grafana | https://grafana.local | https://grafana-dev.local |
| Prometheus の保持 | 15 日・最大 8GB（PVC 10Gi） | 7 日・最大 4GB（PVC 5Gi） |
| Grafana のデータ | PVC 2Gi | PVC 2Gi |
| 秘密情報（Secrets Manager） | `kappaas-apps/monitoring/grafana` | `kappaas-apps/monitoring-dev/grafana` |

- chart: `charts/kube-prometheus-stack`（Application `kube-prometheus-stack`、namespace `monitoring`）
- 環境ごとの値は `charts/kube-prometheus-stack/{prod,dev}/values.yaml`
- Alertmanager は入れていない（アラートの通知は未設定）

## 構成

| コンポーネント | 内容 |
|---|---|
| Prometheus（Operator 管理） | すべての namespace の ServiceMonitor・PodMonitor・PrometheusRule を拾う |
| node-exporter（DaemonSet） | 各ノードの CPU・メモリ・ディスク・ネットワーク |
| kube-state-metrics | Deployment・Pod などの状態 |
| Grafana | データソースは Prometheus。標準のダッシュボード（Kubernetes / Compute Resources、Node Exporter など）入り |
| `templates/external-secret.yaml` | SecretStore と ExternalSecret → Secret `grafana-secrets`（admin のパスワード・OAuth の Client secret） |
| `templates/grafana-httpproxy.yaml` | Certificate `grafana-tls`（ClusterIssuer `k8s-ca`）と Contour の HTTPProxy（TLS は Contour で終端） |

取っていないもの: etcd・kube-scheduler・kube-controller-manager・kube-proxy。kubeadm の既定ではメトリクスを 127.0.0.1 でしか待ち受けないため、values で無効にしている（有効にするにはノード側の設定変更が要る）。

## ログイン

- GitHub でログインする。ログインできるのは GitHub アカウント `cupperservice` だけで、Admin になる（`values.yaml` の `role_attribute_path`。それ以外は `role_attribute_strict` で拒否）
- OAuth App は Argo CD とは別に環境ごとにある（[setup/03-external.md](../setup/03-external.md)）
- 緊急用にローカルの `admin` がある。パスワードは Secrets Manager の `admin-password`

```bash
# admin のパスワードを確認する
kubectl --context dev -n monitoring get secret grafana-secrets -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

## Prometheus を直接見る

Prometheus は外に公開していない。ターゲットやクエリを確認するときは port-forward する。

```bash
kubectl --context dev -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090
# http://localhost:9090/targets
```

## データの置き場所

Prometheus と Grafana のデータは、OpenEBS LocalPV Hostpath（StorageClass `openebs-hostpath`、`charts/openebs-localpv`）でノードのローカルディスク `/var/openebs/local/<PV 名>` に置く。

- **PV は最初に作られたノードに固定される。** 本番でそのノードが止まると、戻るまで Prometheus（または Grafana）も止まる。メトリクスはその間取れない
- ノードを作り直す（ディスクを消す）とデータも消える。PVC を消して作り直せば空の状態で始まる
- `reclaimPolicy: Retain` なので、PVC を消しても PV とノード上のディレクトリは残る。不要なら PV を消し、ノードのディレクトリも手で消す
- 容量の上限は強制されない。Prometheus は `retentionSize` で抑えている

```bash
# どのノードにあるか
kubectl --context prod -n monitoring get pvc
kubectl --context prod get pv -o custom-columns=NAME:.metadata.name,CLAIM:.spec.claimRef.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]
# ノードのディスクの空き
ssh sirius df -h /var
```

### ディスクが逼迫したとき

1. `prod/values.yaml`（または `dev/values.yaml`）の `retention`・`retentionSize` を下げる
2. それでも足りなければ PVC を消して作り直す（過去のメトリクスは消える）

## ダッシュボード

- UI で作ったダッシュボードは Grafana の PVC（SQLite）に残る
- Grafana の Pod を作り直しても残るが、PVC を消すと消える。残したいものは JSON をエクスポートしておく
