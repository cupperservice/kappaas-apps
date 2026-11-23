# 4. 本番クラスタ（sirius / capella / spica）

[02-node.md](02-node.md) を 3 台で済ませてから行う。

> **未検証の手順**: 今の本番は、単一 CP（sirius）で作ったクラスタを後から VIP 付きの 3 台構成にしたもの（[history/2026-10-control-plane-ha.md](../history/2026-10-control-plane-ha.md)）。
> 下の「最初から VIP を使って kubeadm init する」手順は、その記録と kube-vip の公式の手順を合わせて書いたもので、まだ実際には試していない。
> 作り直すときにうまくいかなければ、記録の手順（VIP なしで init → kube-vip を入れて証明書・接続先を VIP に切り替える）で作る。

## 1. sirius: kube-vip を置いてから kubeadm init

### 1-1. kube-vip の static pod

kube-vip は VIP（192.168.3.250）を ARP で広告し、3 台のうち 1 台（リーダー）が VIP を持つ。kubeadm init より前に置く。

kubeadm 1.29 以降は、init の途中では `admin.conf` に権限が無い（init の最後に権限が付く）。そのため **init の間だけ kube-vip に `super-admin.conf` を使わせ**、init 後に普段用の kubeconfig に替える。

**[sirius]**

```bash
export VIP=192.168.3.250 INTERFACE=enp3s0f0 KVVERSION=v1.3.1
sudo ctr image pull ghcr.io/kube-vip/kube-vip:$KVVERSION
sudo ctr run --rm --net-host ghcr.io/kube-vip/kube-vip:$KVVERSION vip /kube-vip \
  manifest pod --interface $INTERFACE --address $VIP \
  --controlplane --arp --leaderElection \
  > /tmp/kube-vip.yaml
sed -i 's#path: /etc/kubernetes/admin.conf#path: /etc/kubernetes/super-admin.conf#' /tmp/kube-vip.yaml
grep -n -A1 hostPath /tmp/kube-vip.yaml        # path: /etc/kubernetes/super-admin.conf
sudo mkdir -p /etc/kubernetes/manifests
sudo cp /tmp/kube-vip.yaml /etc/kubernetes/manifests/kube-vip.yaml
```

- kube-vip のバージョンは 3 台で同じにする。上げるときは https://github.com/kube-vip/kube-vip/releases（`latest` は使わない）

### 1-2. kubeadm の設定

**[sirius]** `~/kubeadm-prod.yaml`

```yaml
apiVersion: kubeadm.k8s.io/v1beta4
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: 192.168.3.124
  bindPort: 6443
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
kubernetesVersion: v1.36.5
controlPlaneEndpoint: "192.168.3.250:6443"
apiServer:
  certSANs:
    - 192.168.3.250
    - 192.168.3.124
    - 192.168.3.126
    - 192.168.3.127
    - sirius
    - capella
    - spica
networking:
  dnsDomain: cluster.local
  podSubnet: 100.64.0.0/16
  serviceSubnet: 172.16.0.0/16
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
# CP にワークロードを同居させるので、ワークロードが使い切っても kubelet・etcd・apiserver・OS が動けるように予約する
# evictionHard は一部だけ書くと残りが既定値にならない場合があるので、5 項目すべて書く
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
```

- clusterName は既定の `kubernetes`。開発は `dev`（Mac の kubeconfig で名前が衝突しないように）
- podSubnet / serviceSubnet は開発と同じ値（別クラスタなので重複は問題ない）
- KubeletConfiguration は `kube-system/kubelet-config` に保存され、join するノードにも入る

### 1-3. init

```bash
# [sirius]
sudo kubeadm init --config ~/kubeadm-prod.yaml --upload-certs
#   最後に control-plane 用の join コマンド（--control-plane --certificate-key 付き）が表示される。控えておく（certificate-key は 2 時間有効）
mkdir -p ~/.kube && sudo cp /etc/kubernetes/admin.conf ~/.kube/config && sudo chown $(id -u):$(id -g) ~/.kube/config
```

### 1-4. kube-vip の kubeconfig を替える

kube-vip は VIP ではなく自ノードの API に直接つなぐ（VIP が無い状態でも起動できるように）。`super-admin.conf` から、`admin.conf` を元にした専用のファイルに替える。

```bash
# [sirius]
sudo sed 's#server: .*#server: https://192.168.3.124:6443#' /etc/kubernetes/admin.conf \
  | sudo tee /etc/kubernetes/kube-vip.conf >/dev/null
sudo chmod 600 /etc/kubernetes/kube-vip.conf
sudo sed -i 's#path: /etc/kubernetes/super-admin.conf#path: /etc/kubernetes/kube-vip.conf#' /etc/kubernetes/manifests/kube-vip.yaml
grep -n -A1 hostPath /etc/kubernetes/manifests/kube-vip.yaml     # path: /etc/kubernetes/kube-vip.conf

ip a show enp3s0f0 | grep 192.168.3.250        # VIP を持っている（deprecated と表示されるのは仕様）
kubectl -n kube-system get pod -o wide | grep kube-vip         # kube-vip-sirius Running
```

## 2. Mac の kubeconfig（コンテキスト `prod`）

**[Mac]**

```bash
ssh sirius sudo cat /etc/kubernetes/admin.conf > ~/.kube/prod.conf && chmod 600 ~/.kube/prod.conf
KUBECONFIG=~/.kube/prod.conf kubectl config rename-context kubernetes-admin@kubernetes prod
grep server: ~/.kube/prod.conf                  # https://192.168.3.250:6443
# 既存の ~/.kube/config があればマージする（無ければ mv ~/.kube/prod.conf ~/.kube/config）
cp ~/.kube/config ~/.kube/config.bak-$(date +%F)
KUBECONFIG=~/.kube/config:~/.kube/prod.conf kubectl config view --flatten > ~/.kube/config.new
mv ~/.kube/config.new ~/.kube/config && chmod 600 ~/.kube/config && rm ~/.kube/prod.conf
kubectl --context prod get nodes                # sirius（Cilium を入れるまで NotReady）
```

- 本番を作り直した場合は、古い `prod` のコンテキスト・クラスタ・ユーザー（`kubernetes-admin`）を `kubectl config delete-*` で消してからマージする（同じ名前があると古いほうが使われる）
- 開発の admin.conf はユーザー名を `dev-admin` に変えてから入れる（[05-dev-cluster.md](05-dev-cluster.md)）

## 3. Cilium

本番・開発とも既定値で入れる（`operator.replicas` だけ開発は 1）。IPAM も既定（cluster-pool）。

```bash
# [Mac]
helm repo add cilium https://helm.cilium.io/
helm repo update
helm --kube-context prod install cilium cilium/cilium --version 1.19.4 -n kube-system
kubectl --context prod wait --for=condition=Ready node/sirius --timeout=300s
```

## 4. capella・spica を control plane として join

1 台ずつ行う。

### 4-1. join

1-3 で表示された control-plane 用のコマンドに、**`--apiserver-advertise-address <そのノードの IP>` を付け足して** 実行する。

```bash
# [capella]（spica は 192.168.3.127）
sudo kubeadm join 192.168.3.250:6443 \
  --token <token> \
  --discovery-token-ca-cert-hash sha256:<hash> \
  --control-plane \
  --certificate-key <certificate-key> \
  --apiserver-advertise-address 192.168.3.126
#   成功時: "This node has joined the cluster and a new control plane instance was created"
```

- token（24 時間）や certificate-key（2 時間）が切れていたら sirius で作り直す

  ```bash
  # [sirius]
  sudo kubeadm init phase upload-certs --upload-certs     # 最後の行が certificate-key
  sudo kubeadm token create --print-join-command          # token と hash
  ```

- **`--print-join-command` の出力はワーカー用。** そのまま実行するとワーカーとして join される。必ず `--control-plane --certificate-key ... --apiserver-advertise-address ...` を付け足す
  （`/etc/kubernetes/admin.conf` ができていなければワーカーとして入っている → `kubeadm reset -f` からやり直し）
- certificate-key は CA の秘密鍵を取り出せる鍵なので扱いに注意

### 4-2. kube-vip を置く

join の後なので `admin.conf` がある。sirius の 1-1 と 1-4 を、接続先をそのノードの IP にして行う。

```bash
# [capella]（spica は 192.168.3.127）
NODE_IP=192.168.3.126
sudo sed "s#server: .*#server: https://$NODE_IP:6443#" /etc/kubernetes/admin.conf \
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

```bash
# [Mac]
kubectl --context prod -n kube-system get pod -o wide | grep -E 'kube-vip|etcd'   # 各ノードで Running（VIP はリーダーの 1 台だけが持つ）
```

## 5. CP にもワークロードを載せる

kubeadm は CP に taint と `node.kubernetes.io/exclude-from-external-load-balancers` ラベルを付ける。
ラベルが残っていると MetalLB がそのノードから LoadBalancer の IP を広告しないので、両方外す。

```bash
# [Mac]
kubectl --context prod taint nodes sirius capella spica node-role.kubernetes.io/control-plane:NoSchedule-
kubectl --context prod label nodes sirius capella spica node.kubernetes.io/exclude-from-external-load-balancers-
kubectl --context prod get nodes -L node.kubernetes.io/exclude-from-external-load-balancers   # 3 台 Ready、列が空
```

## 6. MetalLB・Contour

```bash
# [Mac] リポジトリのルートで
helm repo add metallb https://metallb.github.io/metallb
helm repo add contour https://projectcontour.github.io/helm-charts/
helm repo update

helm --kube-context prod install metallb metallb/metallb --version 0.15.3 -n metallb-system --create-namespace
kubectl --context prod -n metallb-system rollout status deploy/metallb-controller
kubectl --context prod apply -f bootstrap/ip-pool-prod.yaml          # 192.168.3.120

helm --kube-context prod install my-release contour/contour --version 0.5.0 -n projectcontour --create-namespace
kubectl --context prod -n projectcontour get svc                     # EXTERNAL-IP 192.168.3.120
```

- Contour のリリース名は `my-release`（Argo CD などの HTTPProxy はこの Contour を使う）

## 7. 確認

```bash
# [Mac]
kubectl --context prod get nodes -o wide                  # 3 台 Ready、v1.36.5、containerd://2.4.2
kubectl --context prod get pods -A                        # すべて Running
kubectl --context prod get node sirius -o jsonpath='{.status.allocatable}{"\n"}'   # cpu 3500m 前後（予約が効いている）

# etcd が 3 メンバーとも healthy
kubectl --context prod -n kube-system exec etcd-sirius -- etcdctl --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key endpoint health --cluster

# VIP（リーダーのノードを止めると別のノードに移る）
ping -c3 192.168.3.250
```

次: [06-gitops.md](06-gitops.md)（Argo CD）。
