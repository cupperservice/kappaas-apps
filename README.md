# kappaas-apps

## クラスタ構成

| クラスタ | ノード | kubeconfig のコンテキスト | Argo CD | 同期するブランチ | ドキュメント |
|---|---|---|---|---|---|
| 本番 | sirius / capella / spica（CP 3 台固定） | `prod` | https://argocd.local | `release` | [prod-cluster.md](docs/prod-cluster.md) |
| 開発 | altair（1 台） | `dev` | https://argocd-dev.local | `dev` | [dev-cluster.md](docs/dev-cluster.md) |

変更は `dev`（開発クラスタに反映して確認）→ `main`（統合。どこにもデプロイされない）→ `release`（本番に反映）の順にマージする。
業務アプリ（kakeibon-apps）は本番 `main`・開発 `dev`。


## cert-manager の CA　を確認する
### cert-manager が管理する CA Certificate リソースの状態
kubectl get certificate k8s-ca -n cert-manager

### CA cert の詳細（期限・Subject）
kubectl get secret k8s-ca-secret -n cert-manager \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -text \
  | grep -E "Subject:|Issuer:|Not Before|Not After"

### fingerprint 確認
kubectl get secret k8s-ca-secret -n cert-manager \
  -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -fingerprint -sha1


## cloudflare から token を取得する
cloudflared tunnel token --cred-file ./credentials.json k0s-on-prem
