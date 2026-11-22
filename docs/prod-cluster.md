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
- MetalLB: 192.168.3.120、Argo CD: https://argocd.local（root-app は `release` の `applications/overlays/prod` を同期。業務アプリ kakeibon は `services/kakeibon/overlays/prod`）
- 変更は dev → main → release とマージして本番に入れる（release へのマージが本番へのリリース）
- Mac の kubeconfig のコンテキスト名は `prod`（開発は `dev`）
- 2026-11-03〜11-15 は「稼働 3 台 + 待機 1 台を月 1 回入れ替える」運用をしていたが、altair を開発用にするため廃止した
- 実行場所の表記: **[Mac]** = kubectl を使う端末、**[node]** = 対象ノードに SSH

背景: 2026-11-02 に 1 台が暴走して操作不能になり、手動で再起動した（下の「暴走の原因調査」）。

---

## base / overlays 構成・release ブランチへの移行（段階 A・B は 2026-11-23 完了、段階 C は未実施）

本番の root-app を `main` / `applications/base`（旧構成）から `release` / `applications/overlays/prod` に切り替え、
dev に入っていた変更（#8〜#18）を本番に入れた。一度に入れず、次の 3 段階に分けた。

| 段階 | 内容 | 状態 |
|---|---|---|
| A | root-app を release に切り替える（構成だけ。中身は変えない） | 2026-11-23 完了 |
| B | 本番の rolesanywhere スタックを作り、dev → main → release で本番に反映 | 2026-11-23 完了 |
| C | kakeibon-backend のサイドカーを本番に入れ、共用 aws-credential-helper・KppaasRole を廃止 | 未実施 |

**順番の理由**: 段階 A を最初に行う。旧構成の root-app（main / applications/base）が動いている間に新構成を main に入れると、
root-app が環境ごとの値の無い base を同期し、aws-credential-helper（ARN が消える）と cloudflare-tunnel（トンネル設定が消える）が
自動同期で壊れて kakeibon が止まる。段階 A で本番の参照先を release に移してから main を更新する。

### 段階 A: root-app を release に切り替える（2026-11-23 実施）

切り替え時の release は fcbccc1（新構成の骨組みだけ）。中身は今の本番と同じなので、Pod は作り直されない。

1. **[Mac]** 事前確認: 3 台 Ready、etcd の Pod 3 つ Running、Running / Completed 以外の Pod が無い
2. **[Mac]** 差分の確認（読み取りのみ）
   - 本番の各 Application の spec と、release の `applications/overlays/prod` の描画を比較 → 変わるのは targetRevision（main → release）・path・helm.valueFiles だけ
   - 各 chart の `values.yaml` + `prod/values.yaml` を重ねた結果が、main の `values.yaml` と一致。templates・Chart.yaml・argocd-config の base も一致
   - `kubectl --context prod diff -f bootstrap/root-app-prod.yaml` → path と targetRevision の 2 行だけ
3. **[Mac]** 切り替え

   ```bash
   kubectl --context prod apply -f bootstrap/root-app-prod.yaml
   ```

4. **[Mac]** 確認: root-app が `release` / `applications/overlays/prod`、全アプリ Synced / Healthy、app-kakeibon は引き続き kakeibon-apps の main
   - この時点では `argocd-config/applications/overlays/prod/patches/kakeibon-apps-patch.yaml`（#9）は release に無いので効かない。正常

### 段階 B: dev の変更を本番に入れる（2026-11-23 実施）

1. **[AWS]** 本番の rolesanywhere スタックを作る（`aws/rolesanywhere.yaml`、`Environment=prod`、本番の CA）

   ```bash
   kubectl --context prod get secret k8s-ca-secret -n cert-manager -o jsonpath='{.data.tls\.crt}' | base64 -d > ~/k8s-backup/prod-ca.crt
   aws cloudformation deploy --region ap-northeast-1 --stack-name kappaas-prod-rolesanywhere \
     --template-file aws/rolesanywhere.yaml --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides Environment=prod CaCertificate="$(cat ~/k8s-backup/prod-ca.crt)"
   ```

   - 作られたもの: トラストアンカー `a0d8433e-…`・プロファイル `76d3d14c-…`（`kappaas-prod`）、`KappaasExternalSecretsRole`、`KakeibonBackendRole`（段階 C で使う）
   - 既存の KppaasRole・旧トラストアンカー・プロファイルには触れない
2. Outputs の ARN を `charts/external-secrets/prod/values.yaml` に書く（#18）
   - **このとき不具合が見つかった**: #8 でコミットした `charts/external-secrets/templates/aws-config.yaml` に開発の ARN が直接書かれていた。
     そのまま本番に入れると、本番の external-secrets が開発のロールを使って認証に失敗していた。values（`aws.*`）から読む形に直した（#18）
3. 本番に入る差分の確認（release と dev の本番向けの描画を比較）

   | 対象 | 差分 |
   |---|---|
   | applications/overlays/prod | なし |
   | app-kakeibon | 取得元が kakeibon-apps → `services/kakeibon/overlays/prod`。描画は kakeibon-apps main（e5d4188）と完全に一致 |
   | argocd-repositories | ExternalSecret `kappaas-apps-secret` が増える |
   | argocd | Certificate `argocd-server`（argocd.local） |
   | external-secrets | サイドカー・Certificate・ConfigMap（本番の ARN） |
   | それ以外の chart | 変化なし |

   - 本番クラスタへのサーバー側 dry-run で、増える・変わるリソースがすべて受け付けられることを確認
4. dev → main（#19）、main → release（#21）をマージ → 本番が同期
5. 確認

   ```bash
   kubectl --context prod -n argocd get app                                   # 全 Synced / Healthy
   kubectl --context prod -n external-secrets get pods -l app.kubernetes.io/name=external-secrets   # 2/2（サイドカー）
   kubectl --context prod get secretstore,externalsecret -A                    # Ready / SecretSynced
   ```

   - external-secrets が 2/2、`kappaas-apps-secret` が新しいロール（KappaasExternalSecretsRole）で同期できた。ExternalSecret 5 つすべて SecretSynced
   - kakeibon の Pod は作り直されず、https://kakeibon.kappa-as.com は 200、ログインできる
6. `argocd` アプリを手動 Sync（Certificate `argocd-server`）→ Mac で本番の k8s-ca を信頼（[argocd-self-managed.md](argocd-self-managed.md)「サーバー証明書」）→ https://argocd.local が鍵マーク
7. 古い Secret `kappaas-apps-repo`（手で入れていたリポジトリの認証情報）を削除

戻し方（段階 B の後に問題が出た場合）: main → release のマージを revert する PR を release に入れると、本番は fcbccc1 の状態に戻る。
external-secrets で問題が出ても、作成済みの Secret は残るので kakeibon はすぐには止まらない。

### 段階 C: kakeibon-backend のサイドカーと共用 helper の廃止（C-1〜C-3・C-5 は 2026-11-23、C-4 は未実施）

[iam-roles-anywhere.md](iam-roles-anywhere.md) の段階 2 の本番分とステップ 4・5。

1. **C-1（#23）**: `services/kakeibon/overlays/dev` にあったサイドカー一式を base に移し、ARN だけを環境ごとにした（`overlays/<env>/patches/backend/aws-config-patch.yaml`。本番は `KakeibonBackendRole`）
2. **C-2（#24・#25）**: dev → main → release で本番に反映。backend がサイドカー付きの新しい Pod（2/2）に 1 つずつ入れ替わり、`/readyz`（DynamoDB）が 200、ブラウザでログイン・データ・ファイル・メールが動くことを確認
3. **C-3**: 共用の aws-credential-helper を廃止する（`applications/base/aws-credential-helper.yaml`・`charts/aws-credential-helper/`・Dependabot の docker の設定を削除）
   - Application に finalizer があるので、Argo CD が DaemonSet・Service・Certificate を削除する。Namespace `aws-credential-helper` は残るので、空になったら手で削除する
   - 確認: 各ノードの 9911 番ポートが LAN から応答しない（下のコマンド）
4. **C-4（未実施）**: 1 週間ほど様子を見て、KppaasRole と旧トラストアンカー・プロファイルを削除する（iam-roles-anywhere.md のステップ 5。CloudTrail で KppaasRole が使われていないことを確認してから）
5. **C-5**: kakeibon-apps リポジトリの認証情報（`argocd-config/repositories/base/kakeibon-apps.yaml`）を削除する
   - ExternalSecret の creationPolicy が Owner なので、Secret `kakeibon-apps-secret` も一緒に消える
   - Secrets Manager の `kappaas-apps/repositories/kakeibon-apps/github-personal-access-token` も不要になる（削除は手で。GitHub の PAT も失効させる）

```bash
# [Mac] C-3 の確認
kubectl --context prod get ns aws-credential-helper            # 中身が空になったら kubectl delete ns aws-credential-helper
kubectl --context prod -n aws-credential-helper get all
for ip in 192.168.3.124 192.168.3.126 192.168.3.127; do
  curl -s -o /dev/null -w "$ip: %{http_code}\n" --max-time 3 -X PUT http://$ip:9911/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'                # 000（応答なし）になること
done
```

- [x] 段階 A
- [x] 段階 B
- [x] 段階 C の C-1〜C-3・C-5
- [ ] 段階 C の C-4（KppaasRole と旧トラストアンカー・プロファイルの削除）

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
