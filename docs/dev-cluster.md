# 開発クラスタ（altair 1 台）

## 構成

| 項目 | 本番（[prod-cluster.md](prod-cluster.md)） | 開発 |
|---|---|---|
| ノード | sirius / capella / spica（CP 3 台） | altair（192.168.3.125、CP 1 台・ワークロードも載せる） |
| API | VIP 192.168.3.250:6443（kube-vip） | 192.168.3.125:6443（kube-vip なし） |
| kubeconfig のコンテキスト | `prod` | `dev` |
| MetalLB | 192.168.3.120 | 192.168.3.121 |
| Argo CD | https://argocd.local | https://argocd-dev.local |
| 同期するブランチ（kappaas-apps） | `release` | `dev` |
| 業務アプリ kakeibon のマニフェスト | kakeibon-apps の `main`（本番の release 切り替え後は `services/kakeibon/overlays/prod`） | `services/kakeibon/overlays/dev`（[kakeibon.md](kakeibon.md)） |
| root Application | `bootstrap/root-app-prod.yaml` → `applications/overlays/prod` | `bootstrap/root-app-dev.yaml` → `applications/overlays/dev` |
| 外部公開 | Cloudflare Tunnel（kakeibon.kappa-as.com） | なし（LAN 内のみ） |
| AWS | IAM Roles Anywhere（本番の CA・KppaasRole） | IAM Roles Anywhere（dev の CA・dev 用ロール） |

- 開発クラスタは本番と完全に別のクラスタ（etcd・CA・Argo CD すべて別）。開発で壊しても本番に影響しない
- **開発はノード 1 台なので、replicas は 1・PDB は作らない**（2 つにしても冗長化にならない。replicas 1 で PDB の minAvailable 1 を残すと drain が止まる）
  - Argo CD で管理しているもの: `charts/<chart>/dev/values.yaml`（cert-manager・external-secrets）と `services/kakeibon/overlays/dev/patches/*/deployment-patch.yaml`（kakeibon）で 1 にしている。chart を追加したら dev の values でも 1 にする
  - coredns は kubeadm が作るもので Argo CD の管理外。手で 1 にする（kubeadm でアップグレードした後にも確認する）

    ```bash
    kubectl --context dev -n kube-system scale deploy coredns --replicas=1
    kubectl --context dev get deploy,sts -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,REPLICAS:.spec.replicas | awk 'NR==1 || $3>=2'   # 見出しだけになること
    ```
- 実行場所の表記: **[Mac]** = kubectl / helm を使う端末、**[altair]** = altair に SSH

## 運用フロー

kappaas-apps（このリポジトリ。業務アプリ kakeibon のマニフェスト `services/kakeibon` も含む）:

```
feature ブランチ → dev（altair の Argo CD が同期して確認）→ main（統合。どこにもデプロイされない）→ release（本番が同期）
```

- 本番の kakeibon は、本番の release 切り替えまで kakeibon-apps リポジトリの `main` を同期している（[kakeibon.md](kakeibon.md)「本番の切り替え」）

- インフラの変更（chart のバージョン上げなど）も同じ流れ。Dependabot の PR は dev ブランチに来る（`.github/dependabot.yml` の `target-branch: dev`）
- dev → main、main → release は PR でマージする。release へのマージが本番へのリリースになる
- ディレクトリ構成（共通は base / chart 直下の values.yaml、環境ごとの差分は prod・dev に置く）

  ```
  applications/
  ├── base/
  │   ├── argocd.yaml                         共通の Application（valueFiles: [values.yaml]。targetRevision は patch で上書き）
  │   └── ...
  └── overlays/
      ├── prod/
      │   ├── kustomization.yaml              resources: ../../base + patches を並べる
      │   └── patches/
      │       ├── argocd-patch.yaml           base/argocd.yaml への patch（release ブランチ + prod/values.yaml を追加）
      │       └── ...
      └── dev/
          ├── kustomization.yaml
          └── patches/
              ├── argocd-patch.yaml           dev ブランチ + dev/values.yaml を追加
              ├── cloudflare-tunnel-patch.yaml  $patch: delete（開発には入れない）
              └── ...

  argocd-config/{applications,repositories}/
  ├── base/
  └── overlays/{prod,dev}/                    applications/overlays/{prod,dev} に patches/kakeibon-apps-patch.yaml（app-kakeibon の path・ブランチ）

  services/kakeibon/                          業務アプリ kakeibon（docs/kakeibon.md）
  ├── base/
  └── overlays/{prod,dev}/patches/            テーブル・バケット・JWT・ホスト名

  charts/<chart>/
  ├── Chart.yaml
  ├── values.yaml        prod / dev 共通の値
  ├── prod/values.yaml   本番の値
  └── dev/values.yaml    開発の値（cloudflare-tunnel・aws-credential-helper は開発に入れないので無し）
  ```

  - patch は base のファイルごとに 1 つ、`overlays/<env>/patches/<base のファイル名>-patch.yaml` に置く
  - patch は strategic merge 形式（Application のマニフェストの一部を書く）。`metadata.name` で対象が決まるので kustomization.yaml に target は書かない
  - リスト（`valueFiles`、`sources`）は丸ごと置き換わるので、patch には全要素を書く（例: `valueFiles: [values.yaml, dev/values.yaml]`）
  - Application は base で `values.yaml` を指定し、patch で `prod/values.yaml` / `dev/values.yaml` を後ろに追加する（後ろが優先）
  - 現在、環境で値が違うのは argocd（URL・dex）、external-secrets（サイドカーが使う IAM Roles Anywhere の ARN）、aws-credential-helper（本番のみ。kakeibon-backend 用）、cloudflare-tunnel（本番のみ）
  - base に Application を追加したら、prod・dev の両方に patch を作って kustomization.yaml に追加する
  - 開発用の bootstrap は `bootstrap/*-dev.yaml`
- **Argo CD 自身（`argocd` アプリ）は手動 Sync**（[argocd-self-managed.md](argocd-self-managed.md)）。本番より先に開発で Sync して確認する

---

## 構築手順

### 0. 事前準備

- [ ] **[GitHub]** kappaas-apps に `dev` ブランチを作る（main から）。本番用の `release` ブランチも作る（[prod-cluster.md](prod-cluster.md)「base / overlays 構成への移行」）
- [ ] **[GitHub]** dev 用の OAuth App を作る（Settings → Developer settings → OAuth Apps）
  - Homepage URL: `https://argocd-dev.local`、Authorization callback URL: `https://argocd-dev.local/api/dex/callback`
  - Client ID を `charts/argocd/dev/values.yaml` の `clientID` に書く
  - Client secret は `bootstrap/argocd-dex-secret-dev.yaml`（gitignore 済み）に書く。中身は `bootstrap/argocd-dex-secret.yaml` と同じ形式で、`dex.github.clientSecret` を dev 用の値に、`server.secretkey` は新しく生成する（`openssl rand -base64 32`）
- [ ] **[Mac]** `/etc/hosts` に `192.168.3.121 argocd-dev.local`

### 1. altair の状態確認と掃除

altair はローテーションの待機ノードだったので `kubeadm reset` 済み。kubeadm / kubelet / containerd は本番と同じバージョン（v1.36.5 / 2.4.2）のはず。

**[altair]**

```bash
kubeadm version -o short                        # v1.36.5
kubelet --version                               # v1.36.5
containerd --version                            # 2.4.2
test ! -f /etc/kubernetes/kubelet.conf && echo "reset 済み"
ls /etc/kubernetes/manifests                    # 空（kube-vip.yaml も無いこと）
sudo crictl rmp -fa
sudo rm -rf /etc/cni/net.d
```

### 2. kubeadm init

**[altair]** `~/kubeadm-dev.yaml`

podSubnet / serviceSubnet は本番と同じ値（別クラスタなので重複は問題ない。Cilium の既定 IPAM も同じにしておく）。
`clusterName` を `dev` にするのは、Mac の kubeconfig に入れたときに本番（`kubernetes`）と名前が衝突しないようにするため。

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
# 本番と同じ予約（prod-cluster.md「kubelet のリソース予約」）
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

```bash
sudo kubeadm init --config ~/kubeadm-dev.yaml
mkdir -p ~/.kube && sudo cp /etc/kubernetes/admin.conf ~/.kube/config && sudo chown $(id -u):$(id -g) ~/.kube/config
```

### 3. Mac の kubeconfig にコンテキストを追加

**[Mac]**

```bash
# 本番のコンテキスト名を prod に（初回のみ。現在の名前は kubectl config get-contexts で確認）
kubectl config rename-context "$(kubectl config current-context)" prod

# altair の admin.conf を取り込む
ssh altair sudo cat /etc/kubernetes/admin.conf > ~/.kube/dev.conf && chmod 600 ~/.kube/dev.conf
# ユーザー名を dev-admin に変える（必須。下の注意を参照）
sed -i '' 's/kubernetes-admin/dev-admin/g' ~/.kube/dev.conf
# コンテキスト名を dev に
KUBECONFIG=~/.kube/dev.conf kubectl config rename-context dev-admin@dev dev
cp ~/.kube/config ~/.kube/config.bak-$(date +%F)
KUBECONFIG=~/.kube/config:~/.kube/dev.conf kubectl config view --flatten > ~/.kube/config.new
mv ~/.kube/config.new ~/.kube/config && chmod 600 ~/.kube/config && rm ~/.kube/dev.conf
kubectl config use-context prod
kubectl config get-contexts                     # dev → AUTHINFO dev-admin、prod → kubernetes-admin
kubectl --context dev get nodes                 # altair（CNI を入れる 5 までは NotReady）
kubectl --context prod get nodes                # sirius / capella / spica
```

- kubeadm の admin.conf はユーザー名が両クラスタとも `kubernetes-admin`。そのままマージすると同名のユーザーは先に読んだ本番のものが使われ、
  `dev` コンテキストが本番のクライアント証明書を altair に送って `the server has asked for the client to provide credentials`（401）になる。
  そのため、マージ前に dev 側のユーザー名を `dev-admin` に変える
- 名前を変えずにマージしてしまった場合は、上の取り込み（ssh 〜 rm）をやり直す。既存の `dev` コンテキストが優先されて
  ユーザーは古いままなので、最後に `kubectl config set-context dev --user=dev-admin` で付け替える。バックアップは `~/.kube/config.bak-<日付>`

### 4. ワークロードを載せる

**[Mac]**

```bash
kubectl --context dev taint nodes altair node-role.kubernetes.io/control-plane:NoSchedule-
kubectl --context dev label nodes altair node.kubernetes.io/exclude-from-external-load-balancers-
```

### 5. Cilium / MetalLB / Contour

本番はすべて既定値（`helm get values` が null）で入れている。バージョンを本番に合わせる（2026-11-15 時点）。

| リリース | Namespace | chart | バージョン |
|---|---|---|---|
| cilium | kube-system | cilium/cilium | 1.19.4 |
| metallb | metallb-system | metallb/metallb | 0.15.3 |
| my-release | projectcontour | contour | 0.5.0（app 1.33.4） |

導入前に `helm --kube-context prod list -A` で本番のバージョンを再確認する。

**[Mac]**

```bash
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
```

- contour の chart は本番を入れたときの取得元を確認する（`helm --kube-context prod get metadata my-release -n projectcontour`）。上の repo に 0.5.0 が無ければ本番と同じ取得元から入れる

### 6. Argo CD

[argocd-self-managed.md](argocd-self-managed.md)「クラスタを作り直すとき」の dev 版。

**[Mac]**（リポジトリのルートで、`dev` ブランチを checkout した状態）

```bash
kubectl --context dev create namespace argocd
kubectl --context dev apply -f bootstrap/argocd-dex-secret-dev.yaml      # argocd-secret（dev 用 OAuth App）
cd charts/argocd && helm dependency build \
  && helm --kube-context dev install argocd . -n argocd -f values.yaml -f dev/values.yaml \
  && rm -rf charts Chart.lock && cd -
kubectl --context dev apply -f bootstrap/argo-server-dev.yaml            # HTTPProxy argocd-dev.local
kubectl --context dev apply -f bootstrap/root-app-dev.yaml
# external-secrets が入って CRD ができてから（kubectl --context dev get crd secretstores.external-secrets.io）
kubectl --context dev apply -f bootstrap/argocd-secrets.yaml             # SecretStore argocd-secret-store
```

- リポジトリの認証情報（kappaas-apps・kakeibon-apps）は `argocd-config/repositories` の ExternalSecret が Secrets Manager から作る。手で apply しない
  （kappaas-apps は public なので、認証情報が無くても root-app は同期できる）

- root-app-dev が `applications/overlays/dev` を同期し、cert-manager・external-secrets などが入る（cloudflare-tunnel は入らない）
- この時点では aws-credential-helper の ARN が未設定（`REPLACE_WITH_...`）なので、aws-credential-helper と external-secrets の SecretStore はエラーになる。次の 7 で直す
- `argocd` アプリは OutOfSync で現れる。APP DIFF を確認して手動で SYNC

### 7. AWS: dev 用 IAM Roles Anywhere

altair の cert-manager は本番と別の CA を作るので、本番のトラストアンカーでは認証できない。dev 用に作る。

開発では共用の aws-credential-helper（DaemonSet）は使わない。external-secrets のコントローラーの Pod 内のサイドカーが、
CN=`external-secrets` の証明書で `KappaasDevExternalSecretsRole` を引き受ける（[iam-roles-anywhere.md](iam-roles-anywhere.md)）。

1. **[Mac]** dev の CA 証明書を取り出す

   ```bash
   kubectl --context dev get certificate k8s-ca -n cert-manager            # READY True
   kubectl --context dev get secret k8s-ca-secret -n cert-manager \
     -o jsonpath='{.data.tls\.crt}' | base64 -d > ~/k8s-backup/dev-ca.crt
   openssl x509 -in ~/k8s-backup/dev-ca.crt -noout -subject -enddate
   ```

2. **[Mac]** CloudFormation でトラストアンカー・ロール・プロファイルを作る（テンプレート: [aws/rolesanywhere.yaml](../aws/rolesanywhere.yaml)。`Environment=dev`）

   | リソース | 名前（dev） | 内容 |
   |---|---|---|
   | トラストアンカー | `kappaas-dev` | dev の CA 証明書（dev-ca.crt） |
   | ロール | `KappaasDevExternalSecretsRole` | 信頼ポリシー: dev のトラストアンカー（`aws:SourceArn`）かつ CN=`external-secrets`。権限: Secrets Manager の読み取り（dev の既定は `kappaas-apps/repositories/*` のみ） |
   | プロファイル | `kappaas-dev` | KappaasDevExternalSecretsRole |

   ```bash
   # リポジトリのルートで。IAM ロールを作れる権限の AWS プロファイルで実行
   aws cloudformation validate-template --template-body file://aws/rolesanywhere.yaml
   aws cloudformation deploy --region ap-northeast-1 \
     --stack-name kappaas-dev-rolesanywhere \
     --template-file aws/rolesanywhere.yaml \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides Environment=dev CaCertificate="$(cat ~/k8s-backup/dev-ca.crt)"

   aws cloudformation describe-stacks --region ap-northeast-1 --stack-name kappaas-dev-rolesanywhere \
     --query 'Stacks[0].Outputs' --output table      # TrustAnchorArn / ProfileArn / ExternalSecretsRoleArn
   ```

   - 名前と既定の権限は、テンプレートの `Mappings`（`EnvironmentMap`）で環境ごとに決まる。スタックは環境ごとに分ける（`kappaas-<env>-rolesanywhere`）
   - 開発クラスタを作り直して CA が変わったら、新しい dev-ca.crt で同じ deploy を再実行する（トラストアンカーの証明書が置き換わる）
   - 読み取りを許可するシークレットを変えるときは、`SecretArnPatterns` を指定して再実行する（カンマ区切り。指定すると既定は使われないので、必要なものをすべて書く）

     ```bash
     ... --parameter-overrides Environment=dev CaCertificate="$(cat ~/k8s-backup/dev-ca.crt)" \
       SecretArnPatterns="arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:kappaas-apps/repositories/*,arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:<追加するシークレット>/*"
     ```

   - 本番（`Environment=prod`）も同じテンプレートで作る（[iam-roles-anywhere.md](iam-roles-anywhere.md)「段階 1 の手順」）
3. Outputs の 3 つの ARN を `charts/external-secrets/dev/values.yaml` の `aws.trustAnchorArn` / `profileArn` / `roleArn` に書いて dev ブランチに push
4. **[Mac]** 確認

   ```bash
   kubectl --context dev -n external-secrets get certificate external-secrets-aws        # READY True
   kubectl --context dev -n external-secrets get pods -l app.kubernetes.io/name=external-secrets   # READY 2/2
   # サイドカーが引き受けたロール名（KappaasDevExternalSecretsRole）
   kubectl --context dev -n external-secrets exec deploy/external-secrets -c aws-credential-helper -- sh -c '
     T=$(curl -s -X PUT http://127.0.0.1:9911/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
     curl -s -H "X-aws-ec2-metadata-token: $T" http://127.0.0.1:9911/latest/meta-data/iam/security-credentials/; echo'
   kubectl --context dev -n argocd get secretstore,externalsecret           # Ready / SecretSynced
   ```

- 開発の kakeibon-backend には AWS の権限を付けていない（DynamoDB / S3 / SES は使えない）。開発用のテーブル・バケットを用意してから検討する

### 8. 業務アプリ（kakeibon）

`app-kakeibon` が dev ブランチの `services/kakeibon/overlays/dev` を同期する（[kakeibon.md](kakeibon.md)）。

- [x] 開発用の差分（テーブル `kakeibon-dev`、バケット `kakeibo-data-bucket-dev`、JWT `kakeibon-dev/jwt-secret`、ホスト名 `kakeibon-dev.local`）を `services/kakeibon/overlays/dev` に用意
- [x] 開発用の AWS リソース（バケット・JWT・IAM ロール）を作る（[kakeibon-dev-aws.md](kakeibon-dev-aws.md) の手順 2・3）
- [x] kakeibon-backend の AWS 資格情報（iam-roles-anywhere.md の段階 2。サイドカーと KakeibonBackendDevRole）
- [x] **[Mac]** `/etc/hosts` に `192.168.3.121 kakeibon-dev.local`
- [x] https://kakeibon-dev.local で開ける（2026-11-22 は HTTP だけだったが、その後 HTTPS にした。HTTP は HTTPS にリダイレクト）

---

## 確認

```bash
# [Mac]
kubectl --context dev get nodes                         # altair Ready
kubectl --context dev get pods -A                       # 全 Running
kubectl --context dev -n argocd get app                 # 全 Synced / Healthy（cloudflare-tunnel は無い）
kubectl --context dev -n argocd get app -o custom-columns=NAME:.metadata.name,REV:.spec.source.targetRevision   # すべて dev
```

- [ ] https://argocd-dev.local に GitHub でログインできる
- [ ] dev ブランチに軽微な変更を push → 開発だけに反映され、本番（https://argocd.local）には反映されない

## ブラウザから開けないとき

`curl` では開けるのにブラウザでは開けない場合は、ブラウザ側の問題であることが多い（2026-11 に argocd-dev.local・kakeibon-dev.local で発生）。

| 症状 | 原因と対処 |
|---|---|
| `ERR_ADDRESS_UNREACHABLE`。Edge では開けるのに Chrome だけ開けない、など | macOS の「ローカルネットワーク」の許可。「システム設定」→「プライバシーとセキュリティ」→「ローカルネットワーク」に同じアプリ（Google Chrome など）が複数並ぶことがある（補助プログラムや更新のたびに項目が増えるため）。**全部オン**にして ⌘Q で終了 → 起動。それでも駄目なら **Mac を再起動**（どちらの件も再起動で直った） |
| `argocd-dev.local` とだけ入力すると開けない | Argo CD は HTTPS（443）だけ。`https://argocd-dev.local/` と入力する |
| Argo CD で「保護されていない接続」 | Mac が開発の k8s-ca を信頼していない。[argocd-self-managed.md](argocd-self-managed.md)「サーバー証明書」の手順で信頼する。issuer が `O=Argo CD` のままなら、`argocd` アプリを Sync していない（Certificate `argocd-server` が無い） |
| kakeibon-dev.local で「保護されていない接続」 | Argo CD と同じく、Mac が開発の k8s-ca を信頼していない（[argocd-self-managed.md](argocd-self-managed.md)「サーバー証明書」）。証明書は Contour が終端する `kakeibon-client-tls`（`services/kakeibon/overlays/dev/kakeibon-client-tls.yaml`） |
| 名前解決に約 5 秒かかる | `.local` は macOS では mDNS（Bonjour）扱いで、IPv6 を探してから `/etc/hosts` の IPv4 を使うため。初回だけで、正常 |
| `host argocd-dev.local` が NXDOMAIN | `host` は `/etc/hosts` を見ない。`dscacheutil -q host -a name argocd-dev.local` で確認する |
| 古い失敗が残っている | Chrome の `chrome://net-internals/#dns` で Clear host cache、`#sockets` で Flush socket pools |

## 作り直す / 本番に戻す

```bash
# [altair]
sudo kubeadm reset -f
sudo rm -rf /etc/cni/net.d
sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X
```

- 作り直す場合は 2 から。dev の CA が変わるので 7 のトラストアンカーも更新する
- 本番の CP 故障時に altair を本番へ入れる場合は [prod-cluster.md](prod-cluster.md)「CP が故障したとき」
