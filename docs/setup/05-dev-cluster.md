# 5. 開発クラスタ（altair 1 台）

[02-node.md](02-node.md) を altair で済ませてから行う。本番と独立しているので、本番より先に作ってもよい。

## 開発クラスタの決まり

- **ノード 1 台なので replicas は 1・PDB は作らない**（2 つにしても冗長化にならない。replicas 1 で PDB の minAvailable 1 を残すと drain が止まる）
  - Argo CD で入れるもの: `charts/<chart>/dev/values.yaml`（cert-manager・external-secrets）と `services/kakeibon/overlays/dev/patches/*/deployment-patch.yaml` で 1 にしている。chart を追加したら dev の values でも 1 にする
  - coredns は kubeadm が作るので、下の 4 で手で 1 にする（kubeadm でアップグレードした後も確認する）
- kube-vip は使わない（API は altair の IP に直接つなぐ）
- 外部公開（Cloudflare Tunnel）は入れない

## 1. kubeadm init

**[altair]** `~/kubeadm-dev.yaml`

```yaml
apiVersion: kubeadm.k8s.io/v1beta4
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: 192.168.3.125
  bindPort: 6443
---
apiVersion: kubeadm.k8s.io/v1beta4
kind: ClusterConfiguration
clusterName: dev
kubernetesVersion: v1.36.5
controlPlaneEndpoint: "192.168.3.125:6443"
apiServer:
  certSANs:
    - 192.168.3.125
    - altair
networking:
  dnsDomain: cluster.local
  podSubnet: 100.64.0.0/16
  serviceSubnet: 172.16.0.0/16
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
# 本番と同じ予約（04-prod-cluster.md の 1-2）
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

- `clusterName: dev` は、Mac の kubeconfig で本番（`kubernetes`）と名前が衝突しないようにするため

```bash
sudo kubeadm init --config ~/kubeadm-dev.yaml
mkdir -p ~/.kube && sudo cp /etc/kubernetes/admin.conf ~/.kube/config && sudo chown $(id -u):$(id -g) ~/.kube/config
```

## 2. Mac の kubeconfig（コンテキスト `dev`、ユーザー `dev-admin`）

**[Mac]**

```bash
ssh altair sudo cat /etc/kubernetes/admin.conf > ~/.kube/dev.conf && chmod 600 ~/.kube/dev.conf
# ユーザー名を dev-admin に変える（必須。下の注意を参照）
sed -i '' 's/kubernetes-admin/dev-admin/g' ~/.kube/dev.conf
KUBECONFIG=~/.kube/dev.conf kubectl config rename-context dev-admin@dev dev
cp ~/.kube/config ~/.kube/config.bak-$(date +%F)
KUBECONFIG=~/.kube/config:~/.kube/dev.conf kubectl config view --flatten > ~/.kube/config.new
mv ~/.kube/config.new ~/.kube/config && chmod 600 ~/.kube/config && rm ~/.kube/dev.conf
kubectl config use-context prod
kubectl config get-contexts                     # dev → AUTHINFO dev-admin、prod → kubernetes-admin
kubectl --context dev get nodes                 # altair（Cilium を入れるまで NotReady）
```

- kubeadm の admin.conf はユーザー名が両クラスタとも `kubernetes-admin`。そのままマージすると先に読んだ本番のユーザーが使われ、
  `dev` コンテキストが本番のクライアント証明書を altair に送って `the server has asked for the client to provide credentials`（401）になる
- 開発を作り直したときは、古い `dev` コンテキスト・`dev` クラスタ・`dev-admin` ユーザーを `kubectl config delete-context / delete-cluster / delete-user` で消してから取り込む
  （残っていると古いほうが使われる）

## 3. ワークロードを載せる

```bash
# [Mac]
kubectl --context dev taint nodes altair node-role.kubernetes.io/control-plane:NoSchedule-
kubectl --context dev label nodes altair node.kubernetes.io/exclude-from-external-load-balancers-
```

## 4. Cilium・MetalLB・Contour・coredns

バージョンは本番と同じ（[01-overview.md](01-overview.md)）。入れる前に `helm --kube-context prod list -A` で本番のバージョンを確認する。

```bash
# [Mac] リポジトリのルートで
helm repo add cilium https://helm.cilium.io/
helm repo add metallb https://metallb.github.io/metallb
helm repo add contour https://projectcontour.github.io/helm-charts/
helm repo update

# Cilium（ノード 1 台なので operator は 1 つ）
helm --kube-context dev install cilium cilium/cilium --version 1.19.4 -n kube-system --set operator.replicas=1
kubectl --context dev wait --for=condition=Ready node/altair --timeout=300s

# MetalLB
helm --kube-context dev install metallb metallb/metallb --version 0.15.3 -n metallb-system --create-namespace
kubectl --context dev -n metallb-system rollout status deploy/metallb-controller
kubectl --context dev apply -f bootstrap/ip-pool-dev.yaml       # 192.168.3.121

# Contour
helm --kube-context dev install my-release contour/contour --version 0.5.0 -n projectcontour --create-namespace
kubectl --context dev -n projectcontour get svc                 # EXTERNAL-IP 192.168.3.121

# coredns を 1 つに
kubectl --context dev -n kube-system scale deploy coredns --replicas=1
```

## 5. 確認

```bash
# [Mac]
kubectl --context dev get nodes                         # altair Ready
kubectl --context dev get pods -A                       # すべて Running
```

次: [06-gitops.md](06-gitops.md)（Argo CD）。

## 作り直す

```bash
# [altair]
sudo kubeadm reset -f
sudo rm -rf /etc/cni/net.d
sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X
```

- 1 からやり直す。開発の CA（cert-manager の k8s-ca）が変わるので、[06-gitops.md](06-gitops.md) の 4（rolesanywhere スタックの更新）と、Mac での CA の信頼もやり直す
