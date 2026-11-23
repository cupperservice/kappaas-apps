# containerd を全ノード Docker 版 containerd.io 2.4.2 に統一

> **記録**（2026-10 の作業）。現在の構築・運用の手順は [docs/README.md](../README.md)。リンクは整理後の場所に直したが、文中のファイル名や状態は当時のもの。

## 現状（2026-10-25）

| ノード | OS | パッケージ | 入手元 | 作業 |
|---|---|---|---|---|
| sirius | 24.04 | containerd.io 2.4.2-3（noble 用） | Docker（`docker.list`） | hold のみ |
| altair | 26.04 | 2.4.2（noble 用ビルドが残っている可能性） | Docker（アップグレードでリポジトリ無効化の疑い） | A. 26.04 用に入れ直し |
| capella | 26.04 | containerd 2.2.2（Ubuntu 標準） | Ubuntu | B. Docker 版に切替 |
| spica | 26.04 | containerd 2.2.2-0ubuntu1.1（Ubuntu 標準） | Ubuntu | B. Docker 版に切替 |

- Docker リポジトリに 26.04（resolute）用 `containerd.io 2.4.2-3~ubuntu.26.04~resolute` があることを確認済み
- 2.4.2 に「上げる」方向で揃える（sirius / altair を 2.2 に下げるより安全）
- **1 台ずつ** drain → 作業 → 確認 → uncordon
- 実行場所: **[Mac]** = kubectl 端末、**[node]** = 対象ノードに SSH

> **2026-10-25 完了**: capella / spica / altair を入れ替え、全ノード `containerd://2.4.2` / Ready を確認。sirius の hold も実施し、全作業完了。

---

## 共通: Docker リポジトリ（26.04 / deb822 形式）

**対象: spica / capella / altair（26.04 のノードのみ）**。sirius（24.04）は既存の `docker.list`（noble 用）のままで、ここは実施しない。

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
apt-cache policy containerd.io     # Candidate: 2.4.2-3~ubuntu.26.04~resolute
```

古い `docker.list`（noble 用・無効化されたもの）が残っている場合は二重定義になるので削除する:
```bash
ls /etc/apt/sources.list.d/
sudo rm -f /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker.list.distUpgrade
```

### 24.04 → 26.04 にアップグレードしたノードの注意（altair で発生）
アップグレード時に 1 行形式の `.list` が deb822 に自動変換できず、**Kubernetes リポジトリが無効化**されることがある
（`kubernetes.list.disabled` / `kubernetes.list.migrate` が残る）。このままだと kubeadm / kubelet を更新できない。

```bash
ls /etc/apt/sources.list.d/            # kubernetes.list.disabled があれば以下を実施
ls -l /etc/apt/keyrings/kubernetes-apt-keyring.gpg

# pkgs.k8s.io は flat リポジトリなので Suites: / とし、Components は書かない
sudo tee /etc/apt/sources.list.d/kubernetes.sources >/dev/null <<'EOF'
Types: deb
URIs: https://pkgs.k8s.io/core:/stable:/v1.36/deb/
Suites: /
Signed-By: /etc/apt/keyrings/kubernetes-apt-keyring.gpg
EOF
sudo apt-get update
apt-cache policy kubelet | grep -A1 '1.36'      # 500 https://pkgs.k8s.io/... の行があること

# 残骸の削除（拡張子が .list / .sources でないため apt は読まないが、N: 警告が出る）
cd /etc/apt/sources.list.d/
sudo rm -f docker.list.disabled docker.list.migrate kubernetes.list.disabled kubernetes.list.migrate
```
26.04 を新規インストールしたノード（spica）は `kubernetes.list` が有効なままなので不要。

---

## B. capella / spica（Ubuntu 版 → Docker 版）

### 1. drain
```bash
# [Mac]
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
```

### 2. Docker リポジトリ追加
上の「共通」を実施。

### 3. 入れ替え
`containerd.io` は `containerd` / `runc` と競合するため、apt が自動で外す（runc は containerd.io に同梱）。

```bash
# [node]
sudo cp /etc/containerd/config.toml /root/config.toml.bak
sudo apt-get install -y -o Dpkg::Options::=--force-confold containerd.io
```

### 4. 設定確認（重要）
Docker 版の既定 config.toml は **CRI プラグインが無効**（Kubernetes から使えない）。必ず元の設定が残っていることを確認する。

```bash
# [node]
# /root は root 専用なので diff も sudo で実行する
sudo diff /root/config.toml.bak /etc/containerd/config.toml \
  || sudo cp /root/config.toml.bak /etc/containerd/config.toml
grep SystemdCgroup /etc/containerd/config.toml        # SystemdCgroup = true
grep disabled_plugins /etc/containerd/config.toml     # "cri" が含まれていないこと（行が無ければ OK）

sudo apt-mark hold containerd.io
sudo systemctl restart containerd
sudo systemctl restart kubelet

containerd --version                                  # 2.4.2
sudo crictl ps | head                                 # コンテナ一覧が出ること
```

crictl が無い / 接続先の警告が出る場合:
```bash
sudo apt-get install -y cri-tools
echo 'runtime-endpoint: unix:///run/containerd/containerd.sock' | sudo tee /etc/crictl.yaml
```

入れ替え後、`dpkg -l` に `rc containerd`（パッケージは削除済み・設定ファイルだけ残った状態）が出るが無害。
**`sudo apt purge containerd` は実行しない**（`/etc/containerd/config.toml` を消す恐れがある）。

### 5. 確認して戻す
```bash
# [Mac]
kubectl get node <node> -o wide                       # CONTAINER-RUNTIME: containerd://2.4.2 / Ready
kubectl get pods -A -o wide --field-selector spec.nodeName=<node>   # cilium / kube-proxy / csi-smb が Running
kubectl uncordon <node>
```

- [x] capella
- [x] spica

---

## A. altair（Docker 版のまま 26.04 用ビルドに入れ直す）

### 1. 現状確認
```bash
# [altair]
dpkg -l | grep -E 'containerd|runc'
apt-cache policy containerd.io | head -5      # Installed が ~noble なら入れ直し対象
ls /etc/apt/sources.list.d/
```

### 2. 入れ直し
```bash
# [Mac]
kubectl drain altair --ignore-daemonsets --delete-emptydir-data

# [altair] 「共通」で docker.sources を作成（古い docker.list は削除）してから
sudo cp /etc/containerd/config.toml /root/config.toml.bak
sudo apt-mark unhold containerd.io 2>/dev/null   # hold 済みだと入れ直せないため一旦外す
sudo apt-get install -y --reinstall -o Dpkg::Options::=--force-confold \
  containerd.io=2.4.2-3~ubuntu.26.04~resolute
apt-cache policy containerd.io | head -3      # Installed: 2.4.2-3~ubuntu.26.04~resolute
```

### 3. 以降は B の 4〜5 と同じ（B-4 の `apt-mark hold containerd.io` で再び固定する）
- [x] altair（Kubernetes リポジトリも「24.04 → 26.04 にアップグレードしたノードの注意」の手順で作り直し済み）

---

## sirius

24.04 のまま Docker 版 2.4.2 で問題なし。固定だけしておく。
```bash
# [sirius]
sudo apt-mark hold containerd.io
```
- [x] sirius

sirius の 26.04 化（リポジトリの差し替えと containerd.io の入れ直しを含む）は sirius-os-upgrade.md（このリポジトリには無い） を参照。

---

## 最終確認

```bash
# [Mac]
kubectl get nodes -o wide       # 全ノード containerd://2.4.2 / Ready
```
- [x] 全ノード containerd://2.4.2（2026-10-25）
- [x] 各ノード `apt-mark showhold` に containerd.io / kubeadm / kubelet / kubectl

## ロールバック（B で問題が出た場合）
```bash
# [node]
sudo apt-mark unhold containerd.io
sudo apt-get install -y containerd runc          # Ubuntu 版に戻す（containerd.io は自動で外れる）
sudo cp /root/config.toml.bak /etc/containerd/config.toml
sudo systemctl restart containerd kubelet
```
