# Prometheus・Grafana によるメトリクス管理の導入

> **記録**（2026-11 の作業）。現在の運用の手順は [operations/monitoring.md](../operations/monitoring.md)。文中のファイル名や状態は当時のもの。

本番・開発とも metrics-server しかなく、リソース使用量の履歴やダッシュボードが無かった。
SMC による CPU の強制減速（2026-11-17 capella）のような障害を後からメトリクスで追えるように、各クラスタに Prometheus・Grafana を 1 式ずつ入れた。

## 決めたこと

| 項目 | 決定 | 理由・検討した他の案 |
|---|---|---|
| 入れる場所 | 本番・開発に各 1 式 | 2 つのクラスタは完全に別という方針を崩さない。本番に集約する案はクラスタ間の依存ができるのでやめた |
| データの保存先 | OpenEBS LocalPV Hostpath（ノードのローカルディスク） | NAS（SMB / NFS）は Prometheus が非対応で TSDB が壊れるおそれ。Longhorn・Rook-Ceph は Mac mini 7,1（2 コア 4 スレッド）には重く、開発は 1 台なのでレプリカも作れない。local-path-provisioner は Helm リポジトリが無く Dependabot に乗らない |
| PV のノード固定 | 許容する | メトリクスは数日〜2 週間分の監視データで、ノード障害中に見られなくても大きな問題にならない |
| Grafana のログイン | GitHub OAuth（`cupperservice` だけ Admin、ログインフォームなし） | Argo CD と同じ。OAuth App は環境ごと |
| アラート | 入れない（後で別途） | まずメトリクスとダッシュボードだけ |
| コントロールプレーンのメトリクス | 取らない | kubeadm の既定で etcd・scheduler・controller-manager・kube-proxy は 127.0.0.1 待ち受け。ノード側の設定変更が要るので別作業 |

## 経過

| 日付 | PR | 内容 |
|---|---|---|
| 2026-11-24 | #37 | OpenEBS LocalPV Hostpath（`charts/openebs-localpv`、localpv-provisioner 4.6.0）。StorageClass `openebs-hostpath`、Retain、既定クラスにしない、analytics 無効 |
| 2026-11-24 | #38 | kube-prometheus-stack 93.9.0（`charts/kube-prometheus-stack`、namespace `monitoring`）。Grafana の秘密情報は Secrets Manager → ESO。開発のロールで `kappaas-apps/monitoring-dev/*` を読めるようにし、`kappaas-dev-rolesanywhere` スタックを更新 |
| 2026-11-24 | #39 | Grafana の GitHub ログインの修正（下の「問題 1」）と livenessProbe の猶予（「問題 2」） |
| 2026-11-24 | #41 | Grafana のログインフォームを出さない（`auth.disable_login_form`） |
| 2026-11-24 | #40・#42 | dev → main → release。本番に反映 |
| 2026-11-25 | #44 | この記録と、PV のノード固定・NotReady の手順 |
| 2026-11-25 | #47 | 開発の Grafana を止める（下の「問題 4」） |
| 2026-11-25 | （この PR） | altair の PV・etcd・containerd を SSD に移したので、開発の Grafana を戻す |

本番に入れた直後の状態: Prometheus は capella、Grafana は sirius に PV ができた。ターゲット 22 個すべて UP。ノードのメモリ使用率 26〜33%。

## 問題と対処

### 問題 1: Grafana に GitHub でログインすると「Login failed / InternalError」

```
failed to get user info: error getting user teams: Error getting team memberships: unsuccessful response status code 404
```

Grafana は GitHub ログイン時にチーム一覧（`/user/teams`）を取得する。最小権限にしようと OAuth のスコープから `read:org` を外していたため 404 になっていた。`scopes: user:email,read:org` に戻して直った（スコープが増えたので GitHub の承認画面が再度出る）。

### 問題 2: Grafana が初回起動時に 1 回再起動される（開発）

初回起動時の DB の migration に約 2.5 分かかり、その間 `/api/health` が応答しないため、既定の livenessProbe（約 160 秒）で再起動された。再起動後は migration の続きから進み、正常に起動した。
`livenessProbe.failureThreshold: 30`（約 6 分）にして、本番では再起動されなかった。

### 問題 3: 本番の Grafana が約 1 時間止まった（2026-11-25、sirius の LAN ケーブル抜け）

- 16:05 JST に sirius が NotReady（`Kubelet stopped posting node status`）。ping も応答しない
- Grafana の新しい Pod は PV が sirius にあるため Pending（`didn't match PersistentVolume's node affinity`）、古い Pod は Terminating のまま
- etcd は 2/3（sirius が unhealthy）。kakeibon は capella・spica の Pod で動き続け、サービス停止は無かった
- Prometheus（capella）に残っていた sirius の停止直前のメトリクス: load1 は 0.2〜2（最後に 3.35）、空きメモリ約 78%、CPU 温度 49℃。SMC の減速（load 40〜60、ping は通る）とは違い、急に止まった形
- 原因は **LAN ケーブルが抜けていた**。挿し直すと etcd がすぐ 3/3 に戻り、17:06 JST に sirius が Ready、Grafana も sirius で起動した。Terminating の Pod も消えた

わかったこと:

- PV のノード固定は想定どおりに働き、ノードが戻れば何もしなくても復旧した
- 別のノードにある Prometheus で、止まったノードの直前の様子を調べられた
- 気づいたのは Grafana が開けなかったから。アラートが無いので、ノードの NotReady や etcd メンバーの停止を通知できるようにしたい（残りの作業）
- 調べている途中で、`operations/monitoring.md` の PV のノードを調べるコマンドが zsh で動かないことがわかった（`[0]` がファイル名のパターンになる）。引用符で囲むように直した
- NotReady のときは、まず ping で SMC の減速（ping は通る）とケーブル・電源・ハング（ping が通らない）を切り分けるよう `operations/troubleshooting.md` に足した

### 問題 4: 開発の Grafana が altair の HDD を飽和させ、kube-apiserver が落ちた（2026-11-25）

- 16:02 JST に altair が正常にシャットダウンされ（`systemd-poweroff`）、17:09 JST に起動した
- 起動後、OS ディスク（**HDD** `APPLE_HDD_HTS541`）の使用率がほぼ 100%、iowait 60〜70%、読み込み約 40MB/s が続いた
- etcd が遅れ、kube-apiserver が `poststarthook/rbac/bootstrap-roles failed` で再起動を繰り返した（SMC の減速と同じ症状。CPU の周波数・温度は正常で、SMC ではなかった）
- altair で `/proc/<pid>/io` を 10 秒測ると、読み込みは grafana 約 34MB/s、`gpx_grafana-loki` 約 2.5MB/s でほぼすべて。本番の Grafana（SSD）は同じ時期に再起動しても読み込みはほぼ 0 だった
- #47 で開発の Grafana を止めると、iowait が下がり kube-apiserver も安定した
- altair には使っていない 128GB の SSD（古い macOS が入っていた）があったので、OS は HDD のまま PV・etcd・containerd を SSD に移した（bind マウント。[operations/nodes.md](../operations/nodes.md)「altair のディスク」）。コピーは containerd 12G・etcd 394M・PV 399M。移した後、iowait は約 3%、HDD の使用率は 1%、Prometheus の過去のデータも残った
- そのうえで開発の Grafana を戻した

Grafana が HDD のときに何を読み続けていたかは確かめていない（HDD が遅く、Grafana の処理がタイムアウトしてやり直すたびに読み直していた可能性がある）。

## 残りの作業

- 既存のコンポーネントのメトリクス: Argo CD・cert-manager・external-secrets の ServiceMonitor、Contour・Cilium の PodMonitor、ダッシュボードの追加
- アラート通知（Alertmanager）: ノードの NotReady・etcd メンバーの停止など
- （任意）コントロールプレーンのメトリクス: etcd・scheduler・controller-manager・kube-proxy の待ち受けを変える
