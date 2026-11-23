# 2. ノードの準備（4 台共通）

Ubuntu を入れてから kubeadm を実行できる状態にするまで。4 台（sirius・capella・spica・altair）すべてで同じことを行う。
内容は 2026-11-23 時点の spica の設定に合わせている。

## 1. Ubuntu のインストール

- Ubuntu Server 26.04 をインストールする。ユーザーは `cupper`、ホスト名は 01 の表のとおり
- インストーラーで OpenSSH サーバーを入れる
- swap は作らない（作った場合は下の 3 で無効にする）

## 2. 固定 IP（netplan）

**[node]** `/etc/netplan/` の設定を 1 つのファイルにまとめる（今のノードには `00-installer-config.yaml` と `50-cloud-init.yaml` の 2 つが残っているが、新しく作るときは 1 つにする）。
例は spica（IP は 01 の表のとおりに変える。ルーター・DNS の IP は例なので、今のノードの `/etc/netplan/` の値に合わせる）。

```yaml
# /etc/netplan/00-installer-config.yaml
network:
  version: 2
  ethernets:
    enp3s0f0:
      dhcp4: false
      addresses: [192.168.3.127/24]
      routes:
        - to: default
          via: 192.168.3.1          # ルーターの IP
      nameservers:
        addresses: [192.168.3.1]
```

```bash
# cloud-init にネットワーク設定を上書きさせない
echo 'network: {config: disabled}' | sudo tee /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
sudo rm -f /etc/netplan/50-cloud-init.yaml
sudo chmod 600 /etc/netplan/*.yaml
sudo netplan apply
ip -4 a show enp3s0f0
```

## 3. SSH と sudo

**[Mac]** `~/.ssh/config` に 4 台を定義する。**HostName は IP で書く**（スクリプトや手順でこの値を使う）。

```
Host sirius
  HostName 192.168.3.124
  User cupper
  IdentityFile ~/.ssh/<鍵>
# capella（.126）・spica（.127）・altair（.125）も同じ形で
```

```bash
ssh-copy-id -i ~/.ssh/<鍵>.pub <ホスト名>
```

- SSH は **ホスト名** で接続する。IP で接続すると IdentityFile が使われず `Permission denied (publickey,password)` になる

**[node]** Mac から `ssh <ホスト名> sudo ...` で作業できるように、NOPASSWD sudo を設定する（最初の sudo だけパスワードを聞かれる）。

```bash
echo 'cupper ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/k8s-rotate >/dev/null
sudo chmod 440 /etc/sudoers.d/k8s-rotate
sudo visudo -c        # 「parsed OK」が出ること。エラーなら sudo rm /etc/sudoers.d/k8s-rotate
```

- 構文エラーのままセッションを閉じると sudo が使えなくなるので、`visudo -c` が通るまで別の SSH セッションを 1 つ開いたままにする
- ファイル名の `k8s-rotate` は以前の運用の名残（名前に意味はない）
- cupper の SSH 鍵を持つ人は root 権限を得られる。鍵の管理に注意

```bash
# [Mac] 確認
for h in sirius capella spica altair; do ssh -o BatchMode=yes -o ConnectTimeout=5 $h sudo -n true && echo "$h OK"; done
```

## 4. swap・カーネルモジュール・sysctl

**[node]**

```bash
# swap を無効にする（/etc/fstab の swap の行も消す）
sudo swapoff -a
sudo sed -i '/\sswap\s/s/^/#/' /etc/fstab
swapon --show                       # 何も出ないこと

# カーネルモジュール
cat <<'EOF' | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
sudo modprobe overlay
sudo modprobe br_netfilter

# sysctl
cat <<'EOF' | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system
```

- `net.ipv4.conf.all.rp_filter=0` と、`lxc*`・`cilium_*` の rp_filter の設定は Cilium が自分で入れる（`/etc/sysctl.d/99-zzz-override_cilium.conf`）。手で入れなくてよい

## 5. containerd（Docker のリポジトリの containerd.io）

Ubuntu の `containerd` パッケージではなく、Docker のリポジトリの `containerd.io` を使う（全ノードで同じバージョンにそろえるため。経緯は [history/2026-10-containerd-unify.md](../history/2026-10-containerd-unify.md)）。

**[node]**

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: resolute
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
apt-cache policy containerd.io      # 2.4.2-… があること
sudo apt-get install -y containerd.io=<上で確認した 2.4.2 の版>
sudo apt-mark hold containerd.io
```

**Docker 版の既定の config.toml は CRI プラグインが無効**（Kubernetes から使えない）なので、既定の設定を書き出して直す。

```bash
containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd

# 確認（今のノードと同じになること）
grep -nE 'SystemdCgroup|sandbox|disabled_plugins|^version' /etc/containerd/config.toml
#   version = 3
#   disabled_plugins = []
#   sandbox = 'registry.k8s.io/pause:3.10.1'
#   SystemdCgroup = true
```

- `SystemdCgroup` の行が無い場合は、`[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]` に `SystemdCgroup = true` を書く
- `sudo apt purge containerd` は実行しない（`/etc/containerd/config.toml` を消す恐れがある）

## 6. kubeadm・kubelet・kubectl

**[node]**

```bash
KUBE_VERSION=1.36
sudo apt-get install -y apt-transport-https ca-certificates curl gpg
curl -fsSL https://pkgs.k8s.io/core:/stable:/v${KUBE_VERSION}/deb/Release.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${KUBE_VERSION}/deb/ /" \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo apt-get update
apt-cache madison kubeadm | grep 1.36.5      # 版を確認
sudo apt-get install -y kubelet=<1.36.5 の版> kubeadm=<同> kubectl=<同> cri-tools
sudo apt-mark hold kubelet kubeadm kubectl
sudo systemctl enable kubelet
```

- 24.04 から 26.04 にアップグレードしたノードでは、`kubernetes.list` が無効化されることがある（`kubernetes.list.disabled` が残る）。その場合は deb822 形式の `kubernetes.sources` を作る（[history/2026-10-containerd-unify.md](../history/2026-10-containerd-unify.md)「24.04 → 26.04 にアップグレードしたノードの注意」）
- crictl の接続先の警告が出る場合: `echo 'runtime-endpoint: unix:///run/containerd/containerd.sock' | sudo tee /etc/crictl.yaml`

## 確認

```bash
# [node]
apt-mark showhold                    # containerd.io kubeadm kubectl kubelet
ls /etc/apt/sources.list.d/          # docker.sources kubernetes.list ubuntu.sources
containerd --version                 # 2.4.2
kubeadm version -o short             # v1.36.5
sudo crictl info >/dev/null && echo "CRI OK"
lsmod | grep -E 'overlay|br_netfilter'
sysctl net.ipv4.ip_forward net.bridge.bridge-nf-call-iptables     # = 1
```

次: 本番は [04-prod-cluster.md](04-prod-cluster.md)、開発は [05-dev-cluster.md](05-dev-cluster.md)。どちらも先に [03-external.md](03-external.md) を済ませておく（クラスタを作る前でもできる）。
