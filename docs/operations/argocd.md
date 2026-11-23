# Argo CD の運用（Argo CD 自身を charts/argocd で管理する）

## 構成

- Argo CD は `charts/argocd`（argo-cd チャートのラッパー）で管理する。Application は `applications/base/argocd.yaml`（overlay で `prod/values.yaml` / `dev/values.yaml` を足す）
- 設定（argocd-cm / argocd-rbac-cm）は `charts/argocd/values.yaml`（共通）と `prod/values.yaml`・`dev/values.yaml`（URL・dex）に書く。**`kubectl apply` や `kubectl edit` で直接変えない**（次の Sync で戻る）
- `argocd-secret`（dex の GitHub clientSecret、server.secretkey）はチャートで作らない（`configs.secret.createSecret: false`）。構築時に `bootstrap/argocd-dex-secret*.yaml`（gitignore 済み）で入れたものを使う
- **自動同期にしていない。** Argo CD 自身の更新に失敗すると Argo CD が止まり、自分では直せないため、Diff を確認してから手動で Sync する
- リポジトリの認証情報は `argocd-config/repositories` の ExternalSecret が Secrets Manager から作る（手で apply しない）
- 最初の導入（helm install）は [setup/06-gitops.md](../setup/06-gitops.md)

## 更新（Dependabot の PR）

1. PR の差分（argo-cd チャートのバージョン）とリリースノートを確認する
   - チャート: https://github.com/argoproj/argo-helm/releases
   - Argo CD 本体: https://github.com/argoproj/argo-cd/releases
   - **メジャーバージョン（例: 9 → 10）は argo-helm の README の「Upgrading」を必ず読む**
2. PR を dev にマージ
3. 開発（https://argocd-dev.local）で `argocd` アプリの APP DIFF を確認して SYNC。下の「確認」を行う
4. dev → main → release とマージ
5. 本番（https://argocd.local）で同じく APP DIFF → SYNC → 確認

### 確認

```bash
# [Mac]
kubectl --context <env> -n argocd get pods
kubectl --context <env> -n argocd get cm argocd-cm -o jsonpath='{.data.dex\.config}'     # GitHub の connector がある
kubectl --context <env> -n argocd get app                                                # 全アプリ Synced / Healthy
```

- [ ] `argocd` アプリ Synced / Healthy
- [ ] GitHub でログインできる
- [ ] ほかのアプリが Synced のまま

## 戻し方（Sync 後に Argo CD が動かなくなった場合）

Argo CD は自分を直せないので、Mac から helm で前のバージョンに戻す。

```bash
# [Mac] リポジトリのルートで
git checkout <前のコミット> -- charts/argocd
cd charts/argocd
helm dependency build
helm --kube-context prod upgrade argocd . -n argocd -f values.yaml -f prod/values.yaml     # 開発は dev・dev/values.yaml
rm -rf charts Chart.lock
cd - && git checkout HEAD -- charts/argocd
```

Argo CD が戻ったら、release（と main・dev）も前のバージョンに戻す（revert の PR）。戻さないと、次の Sync で壊れたバージョンがまた入る。

- Helm のリリース情報（`secret/sh.helm.release.v1.argocd.*`）は消さない。普段は `helm upgrade` を使わず、戻すときだけ使う

## サーバー証明書

argocd-server のサーバー証明書は、cert-manager がクラスタの CA（ClusterIssuer `k8s-ca`、CN `k8s-on-prem-ca`）で発行する。

- 設定は `charts/argocd/values.yaml` の `argo-cd.server.certificate`（有効期間 90 日・15 日前に更新）と、環境ごとのホスト名（`prod/values.yaml`: `argocd.local`、`dev/values.yaml`: `argocd-dev.local`）
- Certificate `argocd-server` が Secret `argocd-server-tls` を作り、argocd-server は自動でそれを使う（再起動不要。更新も自動）
- HTTPProxy（`bootstrap/argo-server*.yaml`）は TLS パススルーなので、この証明書がそのままブラウザに届く
- Mac で k8s-ca を信頼する手順は [setup/06-gitops.md](../setup/06-gitops.md) の 7

```bash
# [Mac] 届いている証明書の確認（issuer が CN=k8s-on-prem-ca、SAN にホスト名）
echo | openssl s_client -connect 192.168.3.121:443 -servername argocd-dev.local 2>/dev/null | openssl x509 -noout -issuer -ext subjectAltName
```

- issuer が `O=Argo CD` のままなら、`argocd` アプリを Sync していない（Certificate `argocd-server` が無い）
- **注意**: k8s-ca は IAM Roles Anywhere のトラストアンカーでもある。Mac で信頼すると、そのクラスタで Certificate を作れる人は、Mac が信頼する任意のホスト名の証明書を発行できるようになる。クラスタの管理者権限を持つのが自分だけであることが前提

## クラスタの CA を確認する

```bash
# [Mac]
kubectl --context <env> get certificate k8s-ca -n cert-manager
kubectl --context <env> get secret k8s-ca-secret -n cert-manager -o jsonpath='{.data.tls\.crt}' | base64 -d \
  | openssl x509 -noout -subject -issuer -dates -fingerprint -sha1
```

- CA の有効期間は 10 年（`charts/cert-manager/templates/ca-certificate.yaml` の `duration: 87600h`）
