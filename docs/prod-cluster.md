# 本番クラスタ（capella / sirius / spica の 3 台固定）

## 構成

```
             VIP 192.168.3.250:6443 (kube-vip)
      ┌──────────┼──────────┐
   sirius      capella      spica
   CP+etcd     CP+etcd     CP+etcd
   └── 3 台ともワークロードも載せる（control-plane taint なし）──┘
```

| ノード  | IP            | 役割                                         |
| ------- | ------------- | -------------------------------------------- |
| sirius  | 192.168.3.124 | 本番 CP + etcd + ワークロード                |
| capella | 192.168.3.126 | 本番 CP + etcd + ワークロード                |
| spica   | 192.168.3.127 | 本番 CP + etcd + ワークロード                |
| altair  | 192.168.3.125 | **開発クラスタ**（別クラスタ。[dev-cluster.md](dev-cluster.md)） |

- etcd は 3 メンバー。1 台の故障に耐える（2 台止まるとクラスタ全体が停止）
- MetalLB: 192.168.3.120、Argo CD: https://argocd.local（kappaas-apps は `release` を同期。業務アプリ kakeibon は release への切り替えまで kakeibon-apps の `main`、切り替え後は `services/kakeibon/overlays/prod`）
- Mac の kubeconfig のコンテキスト名は `prod`（開発は `dev`）
- 2026-11-03〜11-15 は「稼働 3 台 + 待機 1 台を月 1 回入れ替える」運用をしていたが、altair を開発用にするため廃止した
- 実行場所の表記: **[Mac]** = kubectl を使う端末、**[node]** = 対象ノードに SSH

背景: 2026-11-02 に 1 台が暴走して操作不能になり、手動で再起動した（下の「暴走の原因調査」）。

---

## base / overlays 構成・release ブランチへの移行（1 回だけ・未実施）

本番の root-app を `main` / `applications/base` → `release` / `applications/overlays/prod` に切り替える。

新構成は `release` ブランチに置き、root-app を切り替えるまで本番はどこからも参照しない。
**旧構成の root-app（main / applications/base）が動いている間は、新構成を main に push しない。**
push すると root-app が新しい base（環境ごとの値なし）を同期し、aws-credential-helper（ARN が消える）と
cloudflare-tunnel（トンネル設定が消える）が自動同期で壊れる。

1. 新構成をコミットし、`dev` と `release` ブランチを作って push する（main にはまだ push しない）
   - kakeibon-apps は `dev` ブランチだけ作る（本番は main のまま）
2. **[Mac]** 本番に入る内容を確認（全 Application が `release`・`prod/values.yaml` を使う）
   ```bash
   git checkout release
   kubectl kustomize applications/overlays/prod | grep -E 'name:|targetRevision|values.yaml|path:'
   ```
3. **[Mac]** root-app を切り替える
   ```bash
   kubectl --context prod apply -f bootstrap/root-app-prod.yaml
   ```
4. **[Mac]** 確認
   ```bash
   kubectl --context prod -n argocd get app root-app -o jsonpath='{.spec.source.targetRevision} {.spec.source.path}{"\n"}'   # release applications/overlays/prod
   kubectl --context prod -n argocd get app      # 全 Synced / Healthy（argocd は手動 Sync。APP DIFF が空であること）
   ```
   - Application の定義（targetRevision・helm.valueFiles・path）が変わるだけで、各 chart の値は変更前と同じ
     （共通 + prod を重ねた結果が元の values.yaml と一致することを確認済み）
5. main に push（本番はもう main を見ていないので影響なし）

- [ ] 移行

---

## 設定済みの事項

### 1. SSH と sudo（全ノード）

Mac から SSH（`BatchMode`）で `sudo` を実行する作業（kubelet 設定の反映など）のため、鍵認証と NOPASSWD sudo を設定してある。

- SSH は **ホスト名（sirius / altair / capella / spica）** で接続する。Mac の `~/.ssh/config` に 4 台の Host 定義（HostName・User cupper・IdentityFile）があること。
  IP で接続すると IdentityFile が使われず `Permission denied (publickey,password)` になる
- `HostName` は IP で書く。スクリプトはこの値を join の `--apiserver-advertise-address` と kube-vip.conf の接続先に使う

**[node]**（4 台すべて。`ssh <ホスト名>` で入る。最初の sudo だけパスワードを聞かれる）

```bash
echo 'cupper ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/k8s-rotate >/dev/null
sudo chmod 440 /etc/sudoers.d/k8s-rotate
sudo visudo -c        # 「parsed OK」が出ること。エラーなら sudo rm /etc/sudoers.d/k8s-rotate
```

- 構文エラーのままセッションを閉じると sudo が使えなくなるので、`visudo -c` が通るまで別の SSH セッションを 1 つ開いたままにする
- cupper の SSH 鍵を持つ人は root 権限を得られるようになる。鍵の管理に注意

```bash
# [Mac] 確認（稼働中のノードがすべて OK）
for h in sirius capella spica; do ssh -o BatchMode=yes -o ConnectTimeout=5 $h sudo -n true && echo "$h OK"; done
```

- [x] sirius / capella / spica: 鍵認証 + NOPASSWD sudo

### 2. kubelet のリソース予約（暴走対策）

CP にワークロードが同居しているので、ワークロードがメモリや CPU を使い切っても kubelet・etcd・apiserver・OS が動けるように予約する。
`kube-system/kubelet-config` に入れておけば、以後 join するノードにも自動で反映される。

エディタを使わずに、現在の内容を取り出して末尾に追記し、置き換える。
`evictionHard` は一部だけ書くと、書かなかった項目が既定値を引き継がず無効になる場合があるので、既定の 5 項目をすべて書く。

**[Mac]**

```bash
# 現在の内容をバックアップして取り出す
kubectl -n kube-system get cm kubelet-config -o yaml > ~/k8s-backup/kubelet-config-$(date +%F).yaml
kubectl -n kube-system get cm kubelet-config -o jsonpath='{.data.kubelet}' > /tmp/kubelet.yaml
grep -E '^(systemReserved|kubeReserved|evictionHard):' /tmp/kubelet.yaml    # 何も出ないこと（出たら追記せずに値を直す）

# 予約と退避（eviction）の閾値を追記
cat >> /tmp/kubelet.yaml <<'EOF'
systemReserved:
  cpu: 250m
  memory: 512Mi
kubeReserved:
  cpu: 250m
  memory: 512Mi
evictionHard:
  memory.available: 500Mi
  nodefs.available: 10%
  nodefs.inodesFree: 5%
  imagefs.available: 15%
  imagefs.inodesFree: 5%
EOF

# ConfigMap を置き換える
kubectl -n kube-system create cm kubelet-config --from-file=kubelet=/tmp/kubelet.yaml --dry-run=client -o yaml \
  | kubectl replace -f -
kubectl -n kube-system get cm kubelet-config -o jsonpath='{.data.kubelet}' | tail -14    # 追記した内容が出ること
```

エディタで直接編集したい場合は `KUBE_EDITOR="code --wait" kubectl -n kube-system edit cm kubelet-config` でもよい
（`vi` で `error: there was a problem with the editor "vi"` になる場合）。

**[Mac]** 3 台に 1 台ずつ反映（NOPASSWD sudo 設定済みのこと）

```bash
for h in capella sirius spica; do
  ssh $h 'sudo kubeadm upgrade node phase kubelet-config && sudo systemctl restart kubelet'
  kubectl wait --for=condition=Ready node/$h --timeout=120s
  kubectl get node $h -o jsonpath='{.metadata.name}: cpu {.status.allocatable.cpu} / mem {.status.allocatable.memory}{"\n"}'
done
```

- Allocatable が CPU 3500m 前後、メモリが約 1.5Gi（予約 1Gi + evictionHard 500Mi）減っていれば反映済み
- 故障などで join し直したノードには ConfigMap から自動で反映される

- [x] ConfigMap 更新
- [x] 3 台に反映

---

## CP が故障したとき

予備機は無いので、故障したノードを直して同じノードを join し直す。**故障中は etcd が実質 2 メンバー（耐障害性 0）** なので、
他のノードの再起動・drain などは避け、なるべく早く戻す。

### 短時間で戻る場合（再起動で直る・ハードは無事）

そのまま起動すれば etcd メンバーとして復帰する。`kubectl get nodes` と etcd `endpoint health --cluster` が 3/3 になることを確認する。

### 作り直す場合（ディスク交換・OS 再インストールなど）

1. **[Mac]** 故障ノードを etcd から外す（作り直す前に必ず行う。残したまま join すると etcd が壊れる）

   ```bash
   ETCD=capella        # 稼働中の CP
   kubectl --context prod -n kube-system exec etcd-$ETCD -- etcdctl --endpoints=https://127.0.0.1:2379 \
     --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt \
     --key=/etc/kubernetes/pki/etcd/server.key member list
   #   故障ノードの ID で ... member remove <ID>
   kubectl --context prod delete node <故障ノード>
   ```

2. **[故障ノード]** 修理後、OS・containerd・kubeadm / kubelet をクラスタと同じバージョンにする（[containerd-unify.md](containerd-unify.md)、[worker.md](worker.md)）。
   残骸があれば `sudo kubeadm reset -f` と `/etc/cni/net.d` の削除
3. control-plane として join し、kube-vip を配置する（[control-plane-ha.md](control-plane-ha.md) の 2-2〜2-4。IP はそのノードのもの）
4. **[Mac]** taint と exclude-from-external-load-balancers ラベルを外す（control-plane-ha.md の Phase 4）

   ```bash
   kubectl --context prod taint nodes <node> node-role.kubernetes.io/control-plane:NoSchedule-
   kubectl --context prod label nodes <node> node.kubernetes.io/exclude-from-external-load-balancers-
   ```

5. etcd 3 メンバー healthy、全 Pod Running を確認

修理に時間がかかる場合は、altair（開発クラスタ）を `kubeadm reset` して本番に入れることもできる（開発環境は作り直しになる）。

---

## 暴走の原因調査（2026-11-02 の件）

暴走したノードで実行する（`-b -1` は前回の起動時のログ）。

```bash
sudo journalctl -b -1 -p warning --no-pager | tail -100
sudo journalctl -k -b -1 --no-pager | grep -i -E 'oom|out of memory|hung task|soft lockup|watchdog|I/O error'
sudo journalctl -b -1 -u kubelet --no-pager | grep -i -E 'evict|pressure' | tail -30
```

- [ ] 原因: （記入）。2026-11-17 の capella と同じ症状だった可能性がある（下の「SMC による CPU の強制減速」）

---

## SMC による CPU の強制減速（2026-11-17 capella）

### 症状

- ノードが NotReady（kubelet が `Kubelet stopped posting node status`）。ping・SSH は通る
- load average が 40〜60 台（4 コア）。メモリには余裕がある
- そのノードの kube-apiserver が再起動を繰り返す（自分自身への接続が `TLS handshake timeout` になり、`PostStartHook "rbac/bootstrap-roles" failed` で終了）
  - kubelet は自ノードの apiserver（`192.168.3.12x:6443`）に接続するので、状態を報告できなくなる
- etcd の `endpoint health` でそのノードだけ応答が秒単位に遅い
- そのノードにあった Pod は他のノードに移り、元の Pod は Terminating のまま残る（サービスは継続）

### 原因

本体の管理チップ（SMC）が温度センサーの異常値（CPU 100℃、チップセット 116℃）を報告し、ファンを最大にして CPU を約 220 MHz まで絞った。
CPU 自身のセンサーは 29℃ で、実際には熱くなっていなかった。停止 → 電源を抜いて SMC をリセット → 起動で正常に戻った（周波数 3 GHz 台、SMC の温度 40℃ 前後）。
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

---

## 入れ替え履歴

2026-11-15 にローテーション運用を廃止した（scripts/rotate-cp.sh は削除）。以下はそれまでの記録。

| 日付 | 入った（in） | 休んだ（out） | 備考 |
| ---- | ------------ | ------------- | ---- |
| 2026-11-03 | capella | altair | |
