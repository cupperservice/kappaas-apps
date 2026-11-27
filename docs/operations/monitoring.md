# メトリクス（Prometheus・Grafana）

本番・開発それぞれのクラスタに kube-prometheus-stack を 1 式ずつ入れている（クラスタ間の依存は作らない）。

| | 本番 | 開発 |
|---|---|---|
| Grafana | https://grafana.local | https://grafana-dev.local |
| Prometheus の保持 | 15 日・最大 8GB（PVC 10Gi） | 7 日・最大 4GB（PVC 5Gi） |
| Grafana のデータ | PVC 2Gi | PVC 2Gi |
| 秘密情報（Secrets Manager） | `kappaas-apps/monitoring/grafana`・`kappaas-apps/monitoring/alertmanager` | `kappaas-apps/monitoring-dev/grafana`・`kappaas-apps/monitoring-dev/alertmanager` |

- chart: `charts/kube-prometheus-stack`（Application `kube-prometheus-stack`、namespace `monitoring`）
- 環境ごとの値は `charts/kube-prometheus-stack/{prod,dev}/values.yaml`
- アラートは Alertmanager から Slack に通知する（下の「アラートの通知」）

## 構成

| コンポーネント | 内容 |
|---|---|
| Prometheus（Operator 管理） | すべての namespace の ServiceMonitor・PodMonitor・PrometheusRule を拾う |
| node-exporter（DaemonSet） | 各ノードの CPU・メモリ・ディスク・ネットワーク |
| kube-state-metrics | Deployment・Pod などの状態 |
| Grafana | データソースは Prometheus。標準のダッシュボード（Kubernetes / Compute Resources、Node Exporter など）入り |
| Alertmanager（Operator 管理） | Prometheus のアラートを Slack に送る。PV は使わない |
| `templates/external-secret.yaml` | SecretStore と ExternalSecret → Secret `grafana-secrets`（admin のパスワード・OAuth の Client secret）、`alertmanager-slack`（Slack の Webhook URL） |
| `templates/grafana-httpproxy.yaml` | Certificate `grafana-tls`（ClusterIssuer `k8s-ca`）と Contour の HTTPProxy（TLS は Contour で終端） |

取っていないもの: etcd・kube-scheduler・kube-controller-manager・kube-proxy。kubeadm の既定ではメトリクスを 127.0.0.1 でしか待ち受けないため、values で無効にしている（有効にするにはノード側の設定変更が要る）。

## ログイン

- GitHub でログインする。ログインできるのは GitHub アカウント `cupperservice` だけで、Admin になる（`values.yaml` の `role_attribute_path`。それ以外は `role_attribute_strict` で拒否）
- OAuth App は Argo CD とは別に環境ごとにある（[setup/03-external.md](../setup/03-external.md)）
- ID・パスワードのログインフォームは出さない（`values.yaml` の `auth.disable_login_form`）
- 緊急用にローカルの `admin` がある。パスワードは Secrets Manager の `admin-password`
  - フォームが無いので画面からは入れない。API（Basic 認証）では使える
  - GitHub ログインができず画面で操作したいときは、`disable_login_form` を一時的に `false` にして同期する

```bash
# admin のパスワードを確認する
kubectl --context dev -n monitoring get secret grafana-secrets -o jsonpath='{.data.admin-password}' | base64 -d; echo

# admin で API を使う（例: GitHub ログインのユーザー一覧）
kubectl --context dev -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
curl -s -u "admin:<パスワード>" http://localhost:3000/api/users
```

## Prometheus を直接見る

Prometheus は外に公開していない。ターゲットやクエリを確認するときは port-forward する。

```bash
kubectl --context dev -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090
# http://localhost:9090/targets
```

## アラートの通知

Alertmanager が Slack（Incoming Webhook）に送る。設定は `values.yaml` の `alertmanager.config`。

- ルールは chart の標準のもの（`KubeNodeNotReady`・`TargetDown`・`KubePodCrashLooping` など）と、SMC の誤動作の検知（`additionalPrometheusRulesMap`、[nodes.md](nodes.md) の「SMC による CPU の強制減速」）
- 送らないもの: `Watchdog`（常に発火している生存確認）・`InfoInhibitor`・severity が `none`・`info` のもの
- 同じアラート名・同じ instance をまとめて送る。発火中は 12 時間ごとに再送し、解消したら `[RESOLVED]` を送る
- タイトルに `prod`・`dev` が付く（`prometheusSpec.externalLabels.cluster`）
- Webhook URL は Secrets Manager の `slack-webhook-url`（登録は [setup/03-external.md](../setup/03-external.md)）。シークレットが無いと ExternalSecret が失敗し、Alertmanager の Pod が起動しない
- **Prometheus・Alertmanager が載っているノード自身が止まると通知は来ない。** 開発は 1 台（altair）なので、altair が止まっても通知されない（クラスタ間の依存は作らない方針）

```bash
# Alertmanager の画面（発火中のアラート・一時的な抑止 silence）
kubectl --context dev -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093
# http://localhost:9093

# テスト送信（別のターミナルで）。endsAt を付けないので resolve_timeout（5 分）後に resolved も届く
curl -s -XPOST http://localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d \
  '[{"labels":{"alertname":"TestAlert","severity":"warning","cluster":"dev"},"annotations":{"summary":"テスト送信"}}]'
```

## データの置き場所

Prometheus と Grafana のデータは、OpenEBS LocalPV Hostpath（StorageClass `openebs-hostpath`、`charts/openebs-localpv`）でノードのローカルディスク `/var/openebs/local/<PV 名>` に置く。

- **PV は最初に作られたノードに固定される。** 本番でそのノードが止まると、戻るまで Prometheus（または Grafana）も止まる。メトリクスはその間取れない
- ノードを作り直す（ディスクを消す）とデータも消える。PVC を消して作り直せば空の状態で始まる
- `reclaimPolicy: Retain` なので、PVC を消しても PV とノード上のディレクトリは残る。不要なら PV を消し、ノードのディレクトリも手で消す
- 容量の上限は強制されない。Prometheus は `retentionSize` で抑えている

```bash
# どのノードにあるか（zsh では [0] がファイル名のパターンになるので、-o の引数を引用符で囲む）
kubectl --context prod get pv -o 'custom-columns=PV:.metadata.name,CLAIM:.spec.claimRef.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]'
# 1 つだけ見るなら
kubectl --context prod describe pv <PV 名> | grep -A3 "Node Affinity"
# ノードのディスクの空き
ssh sirius df -h /var
```

### PV のあるノードが止まったとき

新しい Pod は他のノードに移れず Pending になる（イベントは `didn't match PersistentVolume's node affinity`）。古い Pod は Terminating のまま残る。

- **ノードが戻れば何もしなくてよい。** Pending の Pod がそのノードで起動し、Terminating の Pod は消える（2026-11-25 の sirius の例: [history/2026-11-monitoring.md](../history/2026-11-monitoring.md)）
- Prometheus が別のノードにあれば、止まったノードの直前までのメトリクスを見られる（負荷・メモリ・CPU 温度など。調べ方は下の「ノードの障害を調べる」）
- ノードがすぐ戻らず Grafana を先に戻したいときは、Grafana の PVC を消して作り直す（まだ実施したことのない手順。ログインしたユーザーなど Grafana のデータは消える。標準のダッシュボードは chart から作り直される）

```bash
kubectl --context prod -n monitoring delete pvc kube-prometheus-stack-grafana --wait=false   # Pod が使っている間は Terminating のまま
kubectl --context prod -n monitoring delete pod -l app.kubernetes.io/name=grafana --force
# Argo CD が PVC を作り直し、動いているノードに新しい PV ができる。古い PV（Retain）はノードが戻ってから消す
```

### ディスクが逼迫したとき

1. `prod/values.yaml`（または `dev/values.yaml`）の `retention`・`retentionSize` を下げる
2. それでも足りなければ PVC を消して作り直す（過去のメトリクスは消える）

## ノードの障害を調べる

Grafana の Explore、または Prometheus（上の port-forward）でクエリする。`instance` はノードの IP（例 `192.168.3.124:9100`）。

| 見るもの | クエリ |
|---|---|
| 負荷 | `node_load1{instance=~"192.168.3.124.*"}` |
| 空きメモリ（%） | `100 * node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes` |
| CPU 温度 | `node_hwmon_temp_celsius{chip="platform_coretemp_0"}` |
| CPU 周波数（MHz） | `avg by (instance) (node_cpu_scaling_frequency_hertz) / 1e6`（数百 MHz なら SMC による減速。[nodes.md](nodes.md)） |
| 最後に取れた時刻 | `timestamp(up{job="node-exporter"})` |

- `chip="platform_applesmc_768"` の `temp20`（98）・`temp11`（93）は全ノードで同じ値のまま変わらず、実際の温度ではない。CPU 温度は `platform_coretemp_0` を見る

## ダッシュボード

- UI で作ったダッシュボードは Grafana の PVC（SQLite）に残る
- Grafana の Pod を作り直しても残るが、PVC を消すと消える。残したいものは JSON をエクスポートしておく
