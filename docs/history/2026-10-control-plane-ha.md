# コントロールプレーン HA 化手順（CP 3台 + Worker 1台 + kube-vip）

> **記録**（2026-10 の作業）。現在の構築・運用の手順は [docs/README.md](../README.md)。リンクは整理後の場所に直したが、文中のファイル名や状態は当時のもの。

> **2026-11-15 以降の運用**: 本番は sirius / capella / spica の 3 台固定（[prod-cluster.md](../setup/04-prod-cluster.md)）。altair は別の開発クラスタ（[dev-cluster.md](../setup/05-dev-cluster.md)）。
> 下の目標構成（capella = worker、altair = CP）は HA 化時点のもの。

## 目標構成

```
             VIP 192.168.3.250:6443 (kube-vip)
          ┌──────────┼──────────┐
       sirius       altair      spica      capella
       CP+etcd     CP+etcd    CP+etcd     worker
       └── 3台ともワークロードも載せる（taint を外す）──┘
```

| ノード  | IP            | 現在            | 変更後       |
| ------- | ------------- | --------------- | ------------ |
| sirius  | 192.168.3.124 | CP（cordon 中） | CP           |
| altair  | 192.168.3.125 | worker          | **CP** |
| spica   | 192.168.3.127 | worker          | **CP** |
| capella | 192.168.3.126 | worker          | worker       |

- VIP: `192.168.3.250`（MetalLB は 192.168.3.120 を使用中）
- etcd は奇数台が必須。**2メンバーの期間は 1台落ちると全停止**するので、altair の直後に spica を続けて実施する
- 実行場所の表記: **[Mac]** = kubectl を使う端末、**[sirius]** などはそのノードに SSH して実行

---

## Phase 0: 事前準備

### 0-1. 確認事項

- [X] **[ルーター]** 192.168.3.250 が DHCP の配布範囲外であること
- [X] **[sirius]** 各ノードの MAC アドレスが別々であること
  （Mac からは全ノードが同一 MAC `d2:56:f2:e:97:e8` に見えた。中継器の MAC 変換だと kube-vip のフェイルオーバーに影響する可能性あり）
  → 確認済み: ノード側では altair `ac:87:a3:06:da:7d` / capella `78:7b:8a:ac:6a:14` / spica `98:5a:eb:d9:75:10` と別々。
  Mac は中継器経由で MAC 変換されているだけ。フェイルオーバー後に Mac からの接続だけ数秒〜数十秒遅れて戻る可能性があるので、試験はノード上からも確認する

  ```bash
  ping -c1 192.168.3.125; ping -c1 192.168.3.126; ping -c1 192.168.3.127
  ip neigh | grep 192.168.3.12
  ```
- [X] 全ノードが同一物理ホスト上の VM でないこと（同一ホストなら HA の効果は限定的）
- [X] **[sirius]** NIC 名を控える（以降 `<NIC>`）→ sirius は `enp3s0f0`（altair / spica は各ノードで確認）

  ```bash
  ip -br a | grep 192.168.3.124
  ```
- [X] **[Mac]** Cilium が API の IP を直接持っていないこと（`k8sServiceHost` が無ければ OK）→ 確認済み: 設定なし

  ```bash
  helm get values cilium -n kube-system | grep -i k8sService
  ```
- [X] **[Mac]** altair / spica 上に hostPath・local PV のデータが無いこと（`kubeadm reset` 後に戻らない）
  → 確認済み: PV は SMB NAS（ネットワーク）のみ。emptyDir は kakeibon-backend（`bin`）と metrics-server のみで永続データなし。
  kakeibon / coredns は replicas 2（capella にもあり）、metrics-server は 1 のため drain 中に一時的にメトリクスが取れない程度

  ```bash
  kubectl get pv -o wide
  kubectl get pods -A -o wide --field-selector spec.nodeName=altair
  kubectl get pods -A -o wide --field-selector spec.nodeName=spica
  ```
- [X] **[sirius]** crictl を入れる（Phase 1-3 の apiserver 再起動でも使う）→ cri-tools 1.36.0 導入済み

  ```bash
  sudo apt-get install -y cri-tools       # pkgs.k8s.io リポジトリから
  echo 'runtime-endpoint: unix:///run/containerd/containerd.sock' | sudo tee /etc/crictl.yaml
  sudo crictl ps | head
  ```
- [X] **[sirius]** kube-apiserver の再起動回数が多い（106回）原因を確認しておく → 原因は DHCP リース喪失（0-1b で対策済み）
  → 2026-10-25: 現在のログは v1.36.5 アップグレード時（02:51〜02:56）のものだけで異常なし。etcd の slow 警告なし。
  過去 106 回分の原因はコンテナ再作成で追えないため、7日分の kubelet ログと metallb-speaker（582回）で確認する
  → 7日分の kubelet ログで判明: sirius から自分自身の IP（192.168.3.124:6443 / :7473）へ **`network is unreachable`**。
  = sirius のネットワーク（リンク or IP アドレス）が繰り返し消えている。2026-10-17 以降 apiserver readiness 失敗 898 回。
  最終発生 2026-10-23 23:13 UTC / 2026-10-24 21:45 UTC。**Phase 1 の前にこれを解消すること**（下の 0-1b）
- [X] **[全ノード]** 0-1b. ネットワーク断の原因調査と対策（2026-10-25 対策完了。数日後の確認のみ残り）

  ```bash
  # 発生時刻前後の全ログ（UTC 表記に注意）
  sudo journalctl --since "2026-10-24 21:40" --until "2026-10-24 21:50" --utc --no-pager | grep -v kubelet | tail -80
  # NIC のリンク断・ドライバのエラー
  sudo journalctl -k --since "7 days ago" | grep -i -E 'enp3s0f0|link is (down|up)|watchdog|tg3|reset' | tail -30
  # IP の取り方（dhcp4: true なら固定 IP に変更する。CP/etcd の IP が変わるとクラスタが壊れる）
  cat /etc/netplan/*.yaml
  networkctl status enp3s0f0
  ip -s link show enp3s0f0              # errors / dropped
  ```

  - [X] 原因の特定 → **DHCP リース喪失**（2026-10-25 確認）

    - sirius の netplan は `dhcp4: true`。systemd-networkd がほぼ毎日 `DHCP lease lost` → 約2分 IP が消える → 同じ IP を再取得
      （10/22 03:39、10/23 02:11、10/23 23:13、10/24 21:45 UTC）
    - カーネルログにリンク断なし（NIC tg3 / ケーブルは正常）。ルーターがリース更新に応答していないと推定
    - 192.168.3.124 は DHCP で配られている = **ルーターの DHCP 配布範囲に .12x が含まれる**。VIP .250 も範囲内でないか再確認
  - [X] **[ルーター]** 全ノードの MAC に IP を予約（または DHCP 配布範囲から .120〜.127 と .250 を外す）

    | ノード  | IP            | MAC               |
    | ------- | ------------- | ----------------- |
    | sirius  | 192.168.3.124 | 78:7b:8a:c9:c1:b6 |
    | altair  | 192.168.3.125 | ac:87:a3:06:da:7d |
    | capella | 192.168.3.126 | 78:7b:8a:ac:6a:14 |
    | spica   | 192.168.3.127 | 98:5a:eb:d9:75:10 |
  - [X] **[全ノード]** netplan を固定 IP に変更（1台ずつ。NIC 名・IP はノードごとに読み替え）

    - 2026-10-25 全ノード完了。NIC は全ノード `enp3s0f0`。netplan のファイル名はノードで異なる:
      sirius / altair = `50-cloud-init.yaml`、capella = `00-installer-config.yaml`（26.04 インストーラ形式、`match: macaddress` + `set-name` あり）、spica も 26.04 インストーラ形式
    - ファイルが複数ある場合は合成されるので、既存ファイルを退避してから 1 ファイルにする
    - 以下は sirius の例（ファイル名・IP は読み替え）

    ```bash
    ip -br a                                   # NIC 名と現在の IP を確認
    # cloud-init にネットワーク設定を上書きさせない
    echo 'network: {config: disabled}' | sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
    sudo cp /etc/netplan/50-cloud-init.yaml /root/50-cloud-init.yaml.bak
    sudo tee /etc/netplan/50-cloud-init.yaml >/dev/null <<'EOF'
    network:
      version: 2
      ethernets:
        enp3s0f0:
          dhcp4: false
          addresses: [192.168.3.124/24]
          routes:
            - to: default
              via: 192.168.3.1
          nameservers:
            addresses: [192.168.3.1]
    EOF
    sudo chmod 600 /etc/netplan/50-cloud-init.yaml
    sudo netplan try          # 120 秒以内に Enter で確定。SSH が切れたら自動で元に戻る
    ```

    確認

    ```bash
    networkctl status enp3s0f0 | grep -E 'Address|Gateway|DHCP'   # "DHCP4 via" が消えていること
    ip route | grep default
    ping -c2 192.168.3.1
    ```
  - [ ] 数日後、`sudo journalctl -u systemd-networkd --since "3 days ago" | grep -i lease` に `lease lost` が出ないこと

  ```bash
  # [Mac] から見る場合（crictl 不要）
  kubectl -n kube-system logs kube-apiserver-sirius --previous --tail 100
  kubectl -n kube-system logs etcd-sirius --tail 500 | grep -i -E 'slow|took too long'
  kubectl -n kube-system describe pod kube-apiserver-sirius | grep -A5 'Last State'

  # [sirius]
  sudo journalctl -u kubelet --since "1 day ago" | grep -i -E 'apiserver|etcd' | tail -50
  sudo crictl logs --tail 100 $(sudo crictl ps -a -q --name kube-apiserver | head -1)
  sudo crictl logs --tail 100 $(sudo crictl ps -q --name etcd) 2>&1 | grep -i -E 'slow|took too long'
  ```

### 0-2. 全ノードを v1.36.5 に揃える

join 時のバージョン差をなくすため。altair は 1.36.5 済み。

**[sirius]**（コントロールプレーン）

```bash
sudo apt-mark unhold kubeadm
sudo apt-get update && sudo apt-get install -y kubeadm=1.36.5-1.1
sudo apt-mark hold kubeadm
sudo kubeadm upgrade plan
sudo kubeadm upgrade apply v1.36.5

sudo apt-mark unhold kubelet kubectl
sudo apt-get install -y kubelet=1.36.5-1.1 kubectl=1.36.5-1.1
sudo apt-mark hold kubelet kubectl
sudo systemctl daemon-reload && sudo systemctl restart kubelet
```

**[capella] [spica]**（1台ずつ。drain → 更新 → uncordon）

```bash
# [Mac]
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data

# [node]
sudo apt-mark unhold kubeadm kubelet kubectl
sudo apt-get update && sudo apt-get install -y kubeadm=1.36.5-1.1 kubelet=1.36.5-1.1 kubectl=1.36.5-1.1
sudo apt-mark hold kubeadm kubelet kubectl
sudo kubeadm upgrade node
sudo systemctl daemon-reload && sudo systemctl restart kubelet

# [Mac]
kubectl uncordon <node>
```

- [X] `kubectl get nodes` で全ノード v1.36.5 / Ready（2026-10-25）

### 0-3. バックアップ（sirius）

```bash
# etcd スナップショット（/var/lib/etcd はホストにマウントされている）
kubectl -n kube-system exec etcd-sirius -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  snapshot save /var/lib/etcd/snapshot-$(date +%F).db

# [sirius] 証明書・設定一式
sudo tar czf /root/k8s-etc-$(date +%F).tgz /etc/kubernetes
# /var/lib/etcd と /root は root 専用なので、* の展開も root で行う
sudo sh -c 'cp /var/lib/etcd/snapshot-*.db /root/'
sudo ls -lh /root/
```

- [X] sirius の /root に `snapshot-2026-10-25.db`（19M）と `k8s-etc-2026-10-25.tgz`（78M）を取得（2026-10-25）

  - tgz には秘密鍵が含まれる。コピー先でも `chmod 600` で保管する
- [X] スナップショットと tar を sirius 以外（Mac など）にもコピーしておく（2026-10-25 Mac に退避済み）

  ```bash
  # [sirius] /root は root 専用なので、いったん自分のホームへ
  mkdir -p ~/k8s-backup && chmod 700 ~/k8s-backup
  sudo cp /root/snapshot-$(date +%F).db /root/k8s-etc-$(date +%F).tgz ~/k8s-backup/
  sudo chown $(id -u):$(id -g) ~/k8s-backup/* && chmod 600 ~/k8s-backup/*
  sha256sum ~/k8s-backup/*

  # [Mac]
  mkdir -p ~/k8s-backup/$(date +%F) && chmod 700 ~/k8s-backup ~/k8s-backup/$(date +%F)
  scp 'cupper@192.168.3.124:k8s-backup/*' ~/k8s-backup/$(date +%F)/
  chmod 600 ~/k8s-backup/$(date +%F)/*
  shasum -a 256 ~/k8s-backup/$(date +%F)/*      # sirius の値と一致すること

  # [sirius] 一時コピーを削除
  rm -rf ~/k8s-backup
  ```

  - tgz には CA などの秘密鍵が含まれる。**Git リポジトリ内やクラウド同期フォルダには置かない**

---

## Phase 1: sirius に VIP を導入

### 1-1. kube-vip 用 kubeconfig

kube-vip は VIP 経由ではなく自ノードの API に直接つなぐ（VIP が無い状態でも起動できるように）。

**[sirius]**

```bash
sudo sed 's#server: .*#server: https://192.168.3.124:6443#' /etc/kubernetes/admin.conf \
  | sudo tee /etc/kubernetes/kube-vip.conf >/dev/null
sudo chmod 600 /etc/kubernetes/kube-vip.conf
```

### 1-2. kube-vip を static pod で配置

バージョンは https://github.com/kube-vip/kube-vip/releases の最新タグを固定で指定する（`latest` は使わない）。
**3台の CP（sirius / altair / spica）すべてで同じバージョンを使う。**

```bash
# 最新タグの確認
curl -s https://api.github.com/repos/kube-vip/kube-vip/releases/latest | grep tag_name
```

**[sirius]**

```bash
export VIP=192.168.3.250
export INTERFACE=enp3s0f0   # sirius の NIC 名
export KVVERSION=v1.3.1     # 2026-10-25 時点の最新（2026-10-19 公開）

sudo ctr image pull ghcr.io/kube-vip/kube-vip:$KVVERSION
sudo ctr run --rm --net-host ghcr.io/kube-vip/kube-vip:$KVVERSION vip /kube-vip \
  manifest pod --interface $INTERFACE --address $VIP \
  --controlplane --arp --leaderElection \
  > /tmp/kube-vip.yaml

# hostPath だけを kube-vip.conf に差し替える（mountPath はそのまま）
sed -i 's#path: /etc/kubernetes/admin.conf#path: /etc/kubernetes/kube-vip.conf#' /tmp/kube-vip.yaml
grep -n -A1 hostPath /tmp/kube-vip.yaml      # path: /etc/kubernetes/kube-vip.conf になっていること

sudo cp /tmp/kube-vip.yaml /etc/kubernetes/manifests/kube-vip.yaml
```

確認

```bash
# [sirius]
ip a show $INTERFACE | grep 192.168.3.250
# [Mac]
ping -c3 192.168.3.250
curl -k https://192.168.3.250:6443/version     # この時点では証明書エラーにならない -k で OK
```

- [X] VIP に ping が通り、`/version` が返る（2026-10-25）
  - `ip a` で VIP に `deprecated` と表示されるのは kube-vip の仕様（VIP を送信元アドレスに使わせない）で正常

### 1-3. apiserver 証明書に VIP を追加 / controlPlaneEndpoint を設定

**[sirius]**

```bash
kubectl -n kube-system get cm kubeadm-config \
  -o jsonpath='{.data.ClusterConfiguration}' > ~/kubeadm.yaml
cp ~/kubeadm.yaml ~/kubeadm.yaml.orig
```

`~/kubeadm.yaml` を編集:

```yaml
apiServer:
  certSANs:
    - 192.168.3.250
    - 192.168.3.124
    - 192.168.3.125
    - 192.168.3.127
    - sirius
    - altair
    - spica
controlPlaneEndpoint: "192.168.3.250:6443"
```

（`apiServer: {}` の行を上記に置き換え、`controlPlaneEndpoint` を追加）

```bash
sudo mkdir -p /root/pki-backup
sudo mv /etc/kubernetes/pki/apiserver.crt /etc/kubernetes/pki/apiserver.key /root/pki-backup/
sudo kubeadm init phase certs apiserver --config ~/kubeadm.yaml

# SAN に VIP が入ったか
sudo openssl x509 -in /etc/kubernetes/pki/apiserver.crt -noout -ext subjectAltName

# apiserver を再起動（kubelet が自動で立ち上げ直す）
sudo crictl stop $(sudo crictl ps -q --name kube-apiserver)
sleep 30
kubectl get nodes

# 変更後の設定をクラスタに保存（以降の join で使われる）
sudo kubeadm init phase upload-config kubeadm --config ~/kubeadm.yaml
```

確認

```bash
# [Mac] -k なしで通ること（CA は kubeconfig のものを使う）
kubectl --server=https://192.168.3.250:6443 get nodes
```

- [X] VIP 経由で kubectl が通る（2026-10-25。SAN と kubeadm-config の controlPlaneEndpoint も確認済み）

### 1-4. 接続先を VIP に切り替え

**[Mac]** kube-proxy

```bash
kubectl -n kube-system edit cm kube-proxy
#   kubeconfig.conf の server: https://192.168.3.124:6443 → https://192.168.3.250:6443
kubectl -n kube-system rollout restart ds kube-proxy
kubectl -n kube-system rollout status ds kube-proxy
```

**[Mac]** cluster-info（join 時の接続先。署名は controller-manager が自動で付け直す）

```bash
kubectl -n kube-public edit cm cluster-info
#   server: https://192.168.3.124:6443 → https://192.168.3.250:6443
```

有効な bootstrap token が無い間は `jws-kubeconfig-...` 署名が付かないが問題ない。Phase 2 の `kubeadm token create` で自動付与される。

**[capella]** kubelet（worker のまま残るノード。altair / spica は Phase 2 で作り直すので不要）

```bash
sudo cp /etc/kubernetes/kubelet.conf /root/kubelet.conf.bak
sudo sed -i 's#server: https://192.168.3.124:6443#server: https://192.168.3.250:6443#' /etc/kubernetes/kubelet.conf
sudo grep 'server:' /etc/kubernetes/kubelet.conf      # 192.168.3.250
sudo systemctl restart kubelet
```

**[sirius]** admin.conf

```bash
sudo sed -i 's#server: https://192.168.3.124:6443#server: https://192.168.3.250:6443#' /etc/kubernetes/admin.conf
```

（sirius の kubelet.conf / controller-manager.conf / scheduler.conf は自ノード直のままでよい）

**[Mac]** 手元の kubeconfig

```bash
CLUSTER=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}')
kubectl config set-cluster "$CLUSTER" --server=https://192.168.3.250:6443
```

- [X] `kubectl get nodes` 全 Ready（2026-10-25、Mac の kubeconfig は VIP 経由）
- [X] `kubectl -n kube-system get pods` に異常なし（`/readyz` も ok）

---

## Phase 2: altair を CP に昇格

### 2-1. ノードを外す

```bash
# [Mac]
kubectl drain altair --ignore-daemonsets --delete-emptydir-data

# [altair]
sudo kubeadm reset -f
sudo rm -rf /etc/cni/net.d
# reset の最後に表示される案内に従い iptables / ipvs を掃除
sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X

# [Mac]
kubectl delete node altair
```

reset で `StopPodSandbox ... DeadlineExceeded` / `Failed to remove containers` が出ることがある
（reset 中に kubelet・cilium が止まり、Pod のネットワーク削除が応答しないため）。設定・証明書の削除は完了しているので、
cni / iptables の掃除後に再起動して残骸を消す:

```bash
# [altair]
sudo reboot
# 再起動後
echo 'runtime-endpoint: unix:///run/containerd/containerd.sock' | sudo tee /etc/crictl.yaml
sudo crictl rmp -fa          # 残った Pod を強制削除
sudo crictl pods             # 空
ip -br link | grep cilium    # 何も出ない
```

### 2-2. join 情報を発行

**[sirius]**

```bash
sudo kubeadm init phase upload-certs --upload-certs     # 最後の行が certificate-key（2時間有効）
sudo kubeadm token create --print-join-command          # token と hash（24時間有効）
```

- **certificate-key**: `upload-certs` の出力の最後の行（64 桁の 16 進数）。暗号化してクラスタに一時保存した CA 等の証明書を
  join 側が取り出すための鍵。2 時間で失効するので、切れたら `upload-certs` を再実行する。CA 秘密鍵を取り出せる鍵なので扱いに注意
- 期限内なら certificate-key / token は Phase 3（spica）でも再利用できる

### 2-3. コントロールプレーンとして join

> **注意**: `--print-join-command` の出力は**ワーカー用**。そのまま実行するとワーカーとして join される（altair で発生）。
> 必ず `--control-plane --certificate-key ... --apiserver-advertise-address ...` の 3 つを付け足す。

**[altair]**

```bash
sudo kubeadm join 192.168.3.250:6443 \
  --token <token> \
  --discovery-token-ca-cert-hash sha256:<hash> \
  --control-plane \
  --certificate-key <certificate-key> \
  --apiserver-advertise-address 192.168.3.125
#   成功時: "This node has joined the cluster and a new control plane instance was created"
#   （/etc/kubernetes/admin.conf が無い場合はワーカーとして join している → reset からやり直し）

mkdir -p ~/.kube && sudo cp /etc/kubernetes/admin.conf ~/.kube/config && sudo chown $(id -u):$(id -g) ~/.kube/config
```

### 2-4. altair に kube-vip を配置

Phase 1-1 / 1-2 と同じ内容。違いは kube-vip.conf の接続先（自ノード .125）だけ。

**[altair]**

```bash
# kube-vip 用 kubeconfig（自ノードの API に直接つなぐ）
sudo sed 's#server: .*#server: https://192.168.3.125:6443#' /etc/kubernetes/admin.conf \
  | sudo tee /etc/kubernetes/kube-vip.conf >/dev/null
sudo chmod 600 /etc/kubernetes/kube-vip.conf

# static pod マニフェスト（バージョンは sirius と同じ）
export VIP=192.168.3.250 INTERFACE=enp3s0f0 KVVERSION=v1.3.1
sudo ctr image pull ghcr.io/kube-vip/kube-vip:$KVVERSION
sudo ctr run --rm --net-host ghcr.io/kube-vip/kube-vip:$KVVERSION vip /kube-vip \
  manifest pod --interface $INTERFACE --address $VIP \
  --controlplane --arp --leaderElection \
  > /tmp/kube-vip.yaml
sed -i 's#path: /etc/kubernetes/admin.conf#path: /etc/kubernetes/kube-vip.conf#' /tmp/kube-vip.yaml
grep -n -A1 hostPath /tmp/kube-vip.yaml        # path: /etc/kubernetes/kube-vip.conf
sudo cp /tmp/kube-vip.yaml /etc/kubernetes/manifests/kube-vip.yaml
```

確認

```bash
# [Mac]
kubectl -n kube-system get pod kube-vip-altair -o wide   # Running（VIP は sirius が保持したままで正常。altair は待機）
```

### 2-5. 確認

```bash
# [Mac]
kubectl get nodes                                   # altair が control-plane / Ready
kubectl -n kube-system get pods -o wide | grep -E 'etcd|apiserver|kube-vip'

kubectl -n kube-system exec etcd-sirius -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  member list -w table                              # 2 メンバー
```

- [X] etcd 2 メンバー（2026-10-25: sirius / altair とも started）
- [X] altair Ready、kube-vip-altair Running（2026-10-25）→ **すぐに Phase 3 へ**（etcd 2 台の間は 1 台落ちると全停止）

---

## Phase 3: spica を CP に昇格

Phase 2 と同じ流れ。違いは IP（192.168.3.127）だけ。注意点は Phase 2 の各項を参照。

### 3-1. ノードを外す

```bash
# [Mac]
kubectl drain spica --ignore-daemonsets --delete-emptydir-data

# [spica]
sudo kubeadm reset -f
sudo rm -rf /etc/cni/net.d
sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X

# [Mac]
kubectl delete node spica
```

reset でタイムアウトが出た場合は 2-1 と同じく `sudo reboot` → `sudo crictl rmp -fa`。

### 3-2. join 情報（期限切れの場合のみ再発行）

```bash
# [sirius]
sudo kubeadm init phase upload-certs --upload-certs     # certificate-key（2時間）
sudo kubeadm token create --print-join-command          # token / hash（24時間）
```

### 3-3. コントロールプレーンとして join

```bash
# [spica]
sudo kubeadm join 192.168.3.250:6443 \
  --token <token> \
  --discovery-token-ca-cert-hash sha256:<hash> \
  --control-plane \
  --certificate-key <certificate-key> \
  --apiserver-advertise-address 192.168.3.127
#   成功時: "... a new control plane instance was created"

mkdir -p ~/.kube && sudo cp /etc/kubernetes/admin.conf ~/.kube/config && sudo chown $(id -u):$(id -g) ~/.kube/config
```

### 3-4. spica に kube-vip を配置

```bash
# [spica]
sudo sed 's#server: .*#server: https://192.168.3.127:6443#' /etc/kubernetes/admin.conf \
  | sudo tee /etc/kubernetes/kube-vip.conf >/dev/null
sudo chmod 600 /etc/kubernetes/kube-vip.conf

export VIP=192.168.3.250 INTERFACE=enp3s0f0 KVVERSION=v1.3.1
sudo ctr image pull ghcr.io/kube-vip/kube-vip:$KVVERSION
sudo ctr run --rm --net-host ghcr.io/kube-vip/kube-vip:$KVVERSION vip /kube-vip \
  manifest pod --interface $INTERFACE --address $VIP \
  --controlplane --arp --leaderElection \
  > /tmp/kube-vip.yaml
sed -i 's#path: /etc/kubernetes/admin.conf#path: /etc/kubernetes/kube-vip.conf#' /tmp/kube-vip.yaml
grep -n -A1 hostPath /tmp/kube-vip.yaml        # path: /etc/kubernetes/kube-vip.conf
sudo cp /tmp/kube-vip.yaml /etc/kubernetes/manifests/kube-vip.yaml
```

### 3-5. 確認

```bash
# [Mac]
kubectl get nodes                                   # spica が control-plane / Ready
kubectl -n kube-system get pods -o wide | grep -E 'etcd|apiserver|kube-vip'   # 各 3 つ

kubectl -n kube-system exec etcd-sirius -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  member list -w table                              # 3 メンバー
```

- [X] etcd 3 メンバー（`member list` で 3 行、すべて started）（2026-10-25）
- [X] spica Ready、kube-vip-spica Running（2026-10-25）

---

## Phase 4: 仕上げ

```bash
# [Mac] CP にもワークロードを載せる
kubectl taint nodes sirius altair spica node-role.kubernetes.io/control-plane:NoSchedule-
kubectl uncordon sirius

kubectl get nodes -o custom-columns=NAME:.metadata.name,ROLES:.metadata.labels,TAINTS:.spec.taints
```

- [X] taint 解除・sirius の uncordon（2026-10-25、3 台とも TAINTS `<none>`）

kubeadm は CP に `node.kubernetes.io/exclude-from-external-load-balancers` ラベルを付ける。
MetalLB はこのラベルのノードから LoadBalancer IP（192.168.3.120）を広告しないため、
**このままだと広告できるのは worker の capella 1 台だけ**になる。CP にもワークロードを載せる構成なので外す:

```bash
# [Mac]
kubectl label nodes sirius altair spica node.kubernetes.io/exclude-from-external-load-balancers-
kubectl get nodes -L node.kubernetes.io/exclude-from-external-load-balancers   # 列が空
kubectl -n metallb-system get servicel2statuses -o wide                         # .120 を広告しているノード
```

- [X] exclude-from-external-load-balancers ラベル削除（2026-10-25。.120 は altair が広告中）

アプリ側（後続対応。このリポジトリの charts / values で実施）

2026-10-25 の調査結果（1 replica のもの・PDB の有無を `kubectl get deploy,sts -A` / `kubectl get pdb -A` で確認）:

| 対象 | 状況 | 1 台停止時の影響 | 対応 |
|---|---|---|---|
| external-secrets | 1 replica + PDB minAvailable 1 で drain 不能だった | — | 済: replicas 2 + topologySpread（charts/external-secrets/values.yaml） |
| cloudflared | replicas 2 だが 2 Pod とも capella（chart 既定は preferred anti-affinity で強制されない） | capella 停止で外部公開が全断 | required podAntiAffinity + PDB maxUnavailable 1（charts/cloudflare-tunnel） |
| cert-manager | controller / webhook / cainjector すべて 1、PDB なし | webhook 停止中は証明書の作成・更新が失敗 | controller・webhook を 2 + PDB + topologySpread（charts/cert-manager/values.yaml）。cainjector は 1 のまま |
| Argo CD | すべて 1 | 同期が止まるだけ。稼働中アプリは影響なし | 不要（HA は redis-ha 等が必要で大掛かり。chart 管理外） |
| metrics-server / metallb-controller / contour / cilium-operator / csi-smb-controller | 1 | 制御系のみ停止。通信経路（DaemonSet）は継続 | 不要 |

- [X] external-secrets（2026-10-25）
- [X] cloudflared: 2 Pod が別ノード（sirius / spica）、PDB の ALLOWED DISRUPTIONS 1（2026-10-25）
- [X] cert-manager: controller（spica / altair）・webhook（spica / sirius）が別ノード、PDB の ALLOWED DISRUPTIONS 1（2026-10-25）

---

## 動作確認（フェイルオーバー試験）

```bash
# [Mac] etcd 健全性
kubectl -n kube-system exec etcd-sirius -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  endpoint health --cluster                          # 3/3 healthy
```

1. [X] VIP を持っているノードを確認: 各 CP で `ip a | grep 192.168.3.250`（sirius）
2. [X] そのノードを `sudo shutdown -h now`
3. [X] [Mac] で `while true; do kubectl get nodes --request-timeout=3s >/dev/null && echo ok || echo NG; sleep 1; done` を流し、数秒で ok に戻ること
4. [X] 別の CP に VIP が移っていること（altair。`kubectl -n kube-system get lease plndr-cp-lock` の holderIdentity でも確認可）
5. [X] 停止したノードを起動 → Ready、etcd 3/3 healthy に戻ること（2026-10-25）
6. [X] Argo CD の同期、cloudflared 経由のアクセスが継続していること（2026-10-25）

---

## ロールバック

### Phase 1 で問題が出た場合（sirius）

```bash
sudo mv /etc/kubernetes/manifests/kube-vip.yaml /root/
sudo cp /root/pki-backup/apiserver.crt /root/pki-backup/apiserver.key /etc/kubernetes/pki/
sudo crictl stop $(sudo crictl ps -q --name kube-apiserver)
sudo kubeadm init phase upload-config kubeadm --config ~/kubeadm.yaml.orig
# kube-proxy / cluster-info / kubelet.conf / admin.conf / Mac の kubeconfig の server を 192.168.3.124 に戻す
```

### Phase 2/3 の join に失敗した場合

```bash
# [失敗したノード]
sudo kubeadm reset -f

# etcd に残ったメンバーを削除（member list で ID を確認）
kubectl -n kube-system exec etcd-sirius -- etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  member remove <MEMBER_ID>

kubectl delete node <node>
# worker として戻す場合は通常の kubeadm join（--control-plane なし）
```

### etcd が壊れた場合

Phase 0-3 のスナップショットから `etcdutl snapshot restore` で復元（sirius 単独構成に戻す）。
