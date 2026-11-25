# ノードの保守

**[Mac]** = kubectl を使う端末、**[node]** = 対象のノードに SSH。

## etcd の状態を見る（本番）

```bash
# [Mac] ETCD は稼働中の CP のどれか
ETCD=sirius
kubectl --context prod -n kube-system exec etcd-$ETCD -- etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key endpoint health --cluster          # 3 つとも healthy
#   member list で ID を確認できる（endpoint health の代わりに member list）
```

## 1 台を再起動する（本番）

**1 台ずつ行う。** 1 台止まっている間は etcd の耐障害性が 0（もう 1 台止まるとクラスタ全体が止まる）。

```bash
# [Mac]
NODE=spica
kubectl --context prod get nodes                    # 3 台 Ready、etcd 3 つ healthy を先に確認
kubectl --context prod drain $NODE --ignore-daemonsets --delete-emptydir-data
ssh $NODE sudo reboot
kubectl --context prod wait --for=condition=Ready node/$NODE --timeout=600s
kubectl --context prod uncordon $NODE
kubectl --context prod get pods -A -o wide | grep -v -E 'Running|Completed'   # 見出しだけ
```

- drain が PDB で止まる場合は、そのアプリの replicas と PDB を確認する（本番は replicas 2 以上・PDB で 1 台ずつ）
- 次のノードに進む前に etcd の health を確認する

## altair（開発）を止める・起動する

開発は必要なときだけ起動してよい。止めても本番には影響しない。

```bash
# [Mac] 止める（Pod は止まるだけで、起動すると元に戻る）
ssh altair sudo poweroff
```

- 起動は本体の電源ボタン。起動後 `kubectl --context dev get nodes` で Ready、`kubectl --context dev -n argocd get app` で Synced / Healthy を確認する
- 止めている間に dev ブランチにマージしたものは、起動後に Argo CD が同期する
- 1 台なので drain は不要（drain しても移す先が無い）
- （任意）kubelet にシャットダウン時の待ち時間を設定すると、poweroff のときに Pod を順に止めるようになる。`kube-system/kubelet-config` に `shutdownGracePeriod: 60s`・`shutdownGracePeriodCriticalPods: 20s` を足し、下の「kubelet の設定を変える」で反映する

## altair のディスク

altair の OS（`/`）は 2.5 インチの HDD（`sda`、`APPLE_HDD_HTS541`）。HDD だと I/O が詰まって etcd が遅れ、kube-apiserver が再起動を繰り返す（2026-11-25。[history/2026-11-monitoring.md](../history/2026-11-monitoring.md) の問題 4）。
そのため、I/O の多い 3 つを 128GB の SSD（`sdb`、`APPLE_SSD_SM0128`）に置き、bind マウントで元のパスに見せている。containerd・kubeadm・OpenEBS の設定（パス）は変えていない。

| パス | SSD 上の場所 | 中身 |
|---|---|---|
| `/var/openebs` | `/mnt/ssd/openebs` | OpenEBS の PV（Prometheus・Grafana のデータ） |
| `/var/lib/etcd` | `/mnt/ssd/etcd` | etcd のデータ |
| `/var/lib/containerd` | `/mnt/ssd/containerd` | イメージ・コンテナの層 |

```bash
# [altair] /etc/fstab の末尾（SSD は LABEL=altair-ssd の ext4）
LABEL=altair-ssd /mnt/ssd ext4 defaults,noatime 0 2
/mnt/ssd/openebs     /var/openebs        none bind 0 0
/mnt/ssd/etcd        /var/lib/etcd       none bind 0 0
/mnt/ssd/containerd  /var/lib/containerd none bind 0 0

# 確認（3 つとも SOURCE が /dev/sdb1[...]）
for d in /var/openebs /var/lib/etcd /var/lib/containerd; do findmnt -M $d -n -o TARGET,SOURCE; done
```

- 移したときの手順: SSD をフォーマット（`mkfs.ext4 -L altair-ssd`）→ `systemctl disable kubelet containerd` して再起動 → `rsync -aHAX --numeric-ids` で 3 つをコピー → 元を `*.hdd-bak` に退避して bind マウント → `systemctl enable kubelet containerd` して再起動
- 戻すとき: fstab の bind の 3 行を消し、`*.hdd-bak` が残っていれば元の名前に戻して再起動
- altair を作り直すときは、SSD に OS ごと入れてもよい（`/` の使用量は 30GB 弱）
- 確認: iowait は `100*avg(rate(node_cpu_seconds_total{mode="iowait"}[5m]))`（HDD だけのときは 60〜70%、SSD に移して約 3%）

## kubelet の設定を変える

kubelet の設定はクラスタの `kube-system/kubelet-config`（ConfigMap）に置き、各ノードに反映する。ConfigMap にあれば、後から join するノードにも入る。

```bash
# [Mac] バックアップを取り、取り出して編集
kubectl --context prod -n kube-system get cm kubelet-config -o yaml > ~/k8s-backup/kubelet-config-$(date +%F).yaml
kubectl --context prod -n kube-system get cm kubelet-config -o jsonpath='{.data.kubelet}' > /tmp/kubelet.yaml
#   /tmp/kubelet.yaml を編集（evictionHard は 5 項目すべて書く）
kubectl --context prod -n kube-system create cm kubelet-config --from-file=kubelet=/tmp/kubelet.yaml --dry-run=client -o yaml \
  | kubectl --context prod replace -f -

# 1 台ずつ反映（NOPASSWD sudo が必要。setup/02-node.md の 3）
for h in capella sirius spica; do
  ssh $h 'sudo kubeadm upgrade node phase kubelet-config && sudo systemctl restart kubelet'
  kubectl --context prod wait --for=condition=Ready node/$h --timeout=120s
done
```

- エディタで直接編集するなら `KUBE_EDITOR="code --wait" kubectl -n kube-system edit cm kubelet-config`
- 今の予約（systemReserved・kubeReserved 各 250m / 512Mi、evictionHard）は [setup/04-prod-cluster.md](../setup/04-prod-cluster.md) の 1-2。Allocatable は CPU 3500m 前後、メモリは約 1.5Gi 減る

## CP が故障したとき（本番）

予備機は無いので、故障したノードを直して同じノードを join し直す。**故障中は etcd が実質 2 メンバー（耐障害性 0）** なので、
他のノードの再起動・drain は避け、なるべく早く戻す。

### 短時間で戻る場合（再起動で直る・ハードは無事）

そのまま起動すれば etcd のメンバーとして戻る。`kubectl get nodes` と etcd の `endpoint health --cluster` が 3/3 になることを確認する。

### 作り直す場合（ディスク交換・OS の入れ直しなど）

1. **[Mac]** 故障ノードを etcd から外す（作り直す前に必ず行う。残したまま join すると etcd が壊れる）

   ```bash
   ETCD=capella        # 稼働中の CP
   kubectl --context prod -n kube-system exec etcd-$ETCD -- etcdctl --endpoints=https://127.0.0.1:2379 \
     --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt \
     --key=/etc/kubernetes/pki/etcd/server.key member list
   #   故障ノードの ID で ... member remove <ID>
   kubectl --context prod delete node <故障ノード>
   ```

2. **[故障ノード]** [setup/02-node.md](../setup/02-node.md) で、クラスタと同じバージョンにする。残骸があれば `sudo kubeadm reset -f` と `/etc/cni/net.d` の削除
3. control plane として join し、kube-vip を置く（[setup/04-prod-cluster.md](../setup/04-prod-cluster.md) の 4。token・certificate-key は作り直す）
4. **[Mac]** taint と exclude-from-external-load-balancers ラベルを外す（同 5）
5. etcd 3 メンバー healthy、すべての Pod が Running を確認

修理に時間がかかる場合は、altair（開発クラスタ）を `kubeadm reset` して本番に入れることもできる（開発環境は作り直しになる）。

## SMC による CPU の強制減速（2026-11-17 capella で発生）

### 症状

- ノードが NotReady（kubelet が `Kubelet stopped posting node status`）。ping・SSH は通る
- load average が 40〜60 台（4 コア）。メモリには余裕がある
- そのノードの kube-apiserver が再起動を繰り返す（自分自身への接続が `TLS handshake timeout` になり、`PostStartHook "rbac/bootstrap-roles" failed` で終了）
  - kubelet は自ノードの apiserver に接続するので、状態を報告できなくなる
- etcd の `endpoint health` でそのノードだけ応答が秒単位に遅い
- そのノードにあった Pod は他のノードに移り、元の Pod は Terminating のまま残る（サービスは継続）

### 原因

本体の管理チップ（SMC）が温度センサーの異常値（CPU 100℃、チップセット 116℃）を報告し、ファンを最大にして CPU を約 220 MHz まで絞った。
CPU 自身のセンサーは 29℃ で、実際には熱くなっていなかった。停止 → 電源を抜いて SMC をリセット → 起動で正常に戻った。
センサー・SMC の一時的な誤動作と考えられるが、ほこりなどで実際に熱がこもっていた可能性も残る。

### 見分け方（そのノードで実行）

```bash
# CPU の周波数。正常なら 800〜3,500 MHz。数百 MHz なら強制減速されている
grep MHz /proc/cpuinfo

# SMC が報告する温度とファン。TC0F・TC0E・TCXC（CPU）、TPCD（チップセット）が 100℃ 前後、ファンが最大（約 4,900 rpm）なら異常
d=/sys/devices/platform/applesmc.768
cat $d/fan1_input
for t in $d/temp*_input; do l=$(cat ${t%_input}_label); case $l in TC0F|TC0E|TCXC|TPCD|TC0P|TC0C) echo "$l $(( $(cat $t)/1000 ))";; esac; done

# 性能を制限している理由。「Active: ThermStatus」が出ていれば熱による制限（正常時は Auto-HWP や MultiCoreTurbo だけ）
sudo turbostat --debug --num_iterations 1 --interval 1 2>&1 | grep CORE_PERF_LIMIT
```

- 正常なノードと比べると分かりやすい（周波数・SMC の温度・ファンの回転数）
- CPU 自身のセンサー（TC0P・TC0C）が低いのに SMC の値だけ高い場合は、センサー・SMC の異常

### 対処

1. **[Mac]** サービスが他のノードで動いていることを確認（`kubectl --context prod get pods -A -o wide | grep -v Running`、kakeibon の外部 URL）
2. **[node]** 停止する: `sudo poweroff`（停止中は etcd が 2 台になり、もう 1 台止まるとクラスタ全体が止まる）
3. **[本体]** SMC をリセットする: 電源ケーブルを抜いて 15 秒待つ → 差し直して 5 秒待つ → 電源ボタン。吸排気口のほこりも確認する
4. **[node] [Mac]** 起動後に確認
   - 周波数が 3 GHz 前後、SMC の温度が 40〜50℃、ファンが最低（約 1,800 rpm）
   - `kubectl --context prod get nodes` で Ready、unreachable の taint が消えている
   - etcd が 3 メンバーとも healthy、Terminating の Pod が消えている

### 再発したら

センサー・SMC・冷却（チップセットのヒートシンクなど）のハードウェア故障を疑う。修理するまでそのノードを本番から外す。
外している間の etcd は 2 台（耐障害性 0）になるので、必要なら altair を本番に入れる（上の「CP が故障したとき」）。

| 日付 | ノード | 対処 | 備考 |
| ---- | ------ | ---- | ---- |
| 2026-11-17 | capella | 停止 → SMC リセット → 起動 | NotReady は 05:31〜20:42 UTC 頃。サービス停止なし |

## ノードが暴走したときの調査

操作できなくなって再起動したノードで、前回の起動時のログを見る（`-b -1`）。2026-11-02 に 1 台で発生（原因は未特定。SMC と同じ症状だった可能性がある）。

```bash
# [node]
sudo journalctl -b -1 -p warning --no-pager | tail -100
sudo journalctl -k -b -1 --no-pager | grep -i -E 'oom|out of memory|hung task|soft lockup|watchdog|I/O error'
sudo journalctl -b -1 -u kubelet --no-pager | grep -i -E 'evict|pressure' | tail -30
grep MHz /proc/cpuinfo        # 今も遅ければ上の SMC を疑う
```
