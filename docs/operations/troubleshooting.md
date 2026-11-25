# トラブルシューティング（今まで起きたこと）

## ブラウザから開けない

`curl` では開けるのにブラウザでは開けない場合は、ブラウザ側の問題であることが多い（2026-11 に argocd-dev.local・kakeibon-dev.local で発生）。

| 症状 | 原因と対処 |
|---|---|
| `ERR_ADDRESS_UNREACHABLE`。Edge では開けるのに Chrome だけ開けない、など | macOS の「ローカルネットワーク」の許可。「システム設定」→「プライバシーとセキュリティ」→「ローカルネットワーク」に同じアプリ（Google Chrome など）が複数並ぶことがある（補助プログラムや更新のたびに項目が増えるため）。**全部オン**にして ⌘Q で終了 → 起動。それでも駄目なら **Mac を再起動**（どちらの件も再起動で直った） |
| `argocd-dev.local` とだけ入力すると開けない | Argo CD は HTTPS（443）だけ。`https://argocd-dev.local/` と入力する |
| 「保護されていない接続」 | Mac がそのクラスタの k8s-ca を信頼していない（[setup/06-gitops.md](../setup/06-gitops.md) の 7）。Argo CD で issuer が `O=Argo CD` のままなら、`argocd` アプリを Sync していない。kakeibon-dev の証明書は Contour が終端する `kakeibon-client-tls` |
| 名前解決に約 5 秒かかる | `.local` は macOS では mDNS（Bonjour）扱いで、IPv6 を探してから `/etc/hosts` の IPv4 を使うため。初回だけで、正常 |
| `host argocd-dev.local` が NXDOMAIN | `host` は `/etc/hosts` を見ない。`dscacheutil -q host -a name argocd-dev.local` で確認する |
| 古い失敗が残っている | Chrome の `chrome://net-internals/#dns` で Clear host cache、`#sockets` で Flush socket pools |

## kubectl --context dev が 401（`the server has asked for the client to provide credentials`）

Mac の kubeconfig で、開発のユーザー名が本番と同じ `kubernetes-admin` になっている（本番の証明書が altair に送られている）。
[setup/05-dev-cluster.md](../setup/05-dev-cluster.md) の 2 で、ユーザー名を `dev-admin` にして取り込み直す。
既存の `dev` コンテキストが残っている場合は、最後に `kubectl config set-context dev --user=dev-admin` で付け替える。

## ノードが NotReady

- まず Mac から ping する（`ping <ノードの IP>`）。**応答が無ければ** LAN ケーブル・電源・OS のハングを疑い、本体を見に行く（2026-11-25 の sirius は LAN ケーブルが抜けていた）。etcd が 2 台で動いているので、他のノードの再起動・drain はしない
- ping が通るなら CPU の周波数を見る（`ssh <node> grep MHz /proc/cpuinfo`）。数百 MHz なら SMC による強制減速（[nodes.md](nodes.md)「SMC による CPU の強制減速」）
- 構築直後（Cilium を入れる前）は NotReady で正常
- kubelet のログ: `ssh <node> sudo journalctl -u kubelet --since -30min --no-pager | tail -50`

## kakeibon の Pod が Ready にならない

backend の `/readyz` は DynamoDB への疎通を、client の `/readyz` は backend を確認する。AWS の認証が通っていないと両方 Ready にならない。

```bash
# [Mac]
kubectl --context <env> -n kakeibon get pods                                # backend は 2/2（サイドカー）
kubectl --context <env> -n kakeibon get certificate                         # kakeibon-backend-aws が READY True
kubectl --context <env> -n kakeibon logs deploy/kakeibon-backend -c aws-credential-helper --tail=30
kubectl --context <env> -n kakeibon get cm kakeibon-backend-aws -o yaml     # ARN がその環境のもの
kubectl --context <env> -n kakeibon get externalsecret                      # SecretSynced
```

- `AccessDenied` なら、ロールの権限（テーブル・バケット名）が ConfigMap `kakeibon-backend-config` と合っているか（[aws-iam.md](aws-iam.md)）
- 信頼ポリシーで拒否されるなら、トラストアンカーの CA が今のクラスタの CA か（作り直した後にスタックを更新していない）
- イメージを取れない（`ImagePullBackOff`）なら、ExternalSecret `kakeibon-ghcr-secret` と GHCR の PAT の期限

## ExternalSecret が SecretSynced にならない

```bash
# [Mac]
kubectl --context <env> get secretstore,externalsecret -A
kubectl --context <env> -n <ns> describe externalsecret <名前>                 # Events に理由
kubectl --context <env> -n external-secrets get pods -l app.kubernetes.io/name=external-secrets   # 2/2
kubectl --context <env> -n external-secrets logs deploy/external-secrets -c aws-credential-helper --tail=30
```

- `AccessDeniedException`: そのシークレットがロールの読み取り範囲に無い（[aws-iam.md](aws-iam.md)「シークレットを追加する」）
- `ResourceNotFoundException`: Secrets Manager の名前の間違い、またはシークレットが無い
- Argo CD の SecretStore `argocd-secret-store` が無い: `bootstrap/argocd-secrets.yaml` を apply していない（[setup/06-gitops.md](../setup/06-gitops.md) の 5）
