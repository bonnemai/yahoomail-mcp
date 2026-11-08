# Déploiement AWS Lambda

Ce document couvre uniquement le déploiement distant. Pour installer le serveur, configurer Yahoo, lancer Apple Container/Docker et tester localement, suivre le [README.md](./README.md).

## Architecture

```text
Client MCP --Bearer token--> API Gateway HTTP API
                                  |
                                  v
                         Lambda + Web Adapter
                           |              |
                           v              v
                    Yahoo IMAP/TLS     DynamoDB
                                      previews TTL
```

La Lambda utilise un token bearer séparé du token local. Les identifiants Yahoo et ce token sont lus depuis Secrets Manager. Le déploiement est en lecture seule par défaut. Avec `EnableRemoteWrites=true`, les écritures sont limitées au déplacement vers Trash après approbation explicite ; les brouillons et l’envoi ne sont pas exposés par le profil distant.

L’URL d’approbation `/approve/...` affiche la preview et demande un clic utilisateur. Les approvals ne nécessitent pas de consulter CloudWatch Logs. Chaque preview est à usage unique et expire après cinq minutes. Le lien d’approbation est lui-même secret : ne pas le publier dans un ticket, un log ou une conversation partagée.

## Prérequis

- AWS CLI configuré avec le compte cible.
- AWS SAM CLI.
- Docker Desktop en fonctionnement.
- Mac Apple Silicon pour l’image ARM64.
- Un compte Yahoo avec un app password déjà créé selon le [README.md](./README.md).

Vérifier le compte et la région :

```sh
aws sts get-caller-identity
aws configure get region
```

Les exemples utilisent `eu-west-2` (Londres). Remplacer cette région partout si nécessaire.

## 1. Créer le secret Yahoo

Créer un token distant distinct du token local :

```sh
openssl rand -hex 32
```

Créer un secret JSON dans Secrets Manager, par exemple avec un fichier temporaire protégé :

```json
{
  "YAHOO_EMAIL": "your-address@yahoo.com",
  "YAHOO_APP_PASSWORD": "your_yahoo_app_password",
  "YAHOO_MCP_TOKEN": "your_remote_token"
}
```

Ne pas envoyer l’app password ou le token dans un prompt d’IA. Le secret doit rester dans Secrets Manager et n’est jamais copié dans le template CloudFormation.

```sh
aws secretsmanager create-secret \
  --name yahoo-mail-mcp/account \
  --region eu-west-2 \
  --secret-string file://yahoo-secret.json
```

Noter l’ARN retourné (`ARN`). Supprimer ensuite le fichier local contenant le secret de manière sûre.

## 2. Valider et déployer

Depuis la racine du projet :

```sh
sam validate --lint --region eu-west-2
sam build --use-container
sam deploy --stack-name yahoo-mail-mcp \
  --region eu-west-2 \
  --resolve-image-repos \
  --capabilities CAPABILITY_IAM \
  --confirm-changeset \
  --parameter-overrides YahooMcpSecretArn=YOUR_SECRET_ARN
```

Le stack crée :

- une API Gateway HTTP avec `/mcp` et, si les écritures sont activées, `/approve/{approval_id}` ;
- une Lambda ARM64 basée sur [Dockerfile.lambda](./Dockerfile.lambda) ;
- une table DynamoDB à la demande avec expiration des previews, uniquement si les écritures sont activées ;
- un rôle autorisé à lire le secret et, si les écritures sont activées, à utiliser la table d’approbation ;
- un groupe de logs conservé sept jours.

Accepter le change set uniquement après avoir vérifié le compte, la région et l’ARN du secret.

## 3. Vérifier l’endpoint

Récupérer l’URL :

```sh
MCP_URL=$(aws cloudformation describe-stacks \
  --stack-name yahoo-mail-mcp \
  --region eu-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`McpUrl`].OutputValue' \
  --output text)
printf '%s\n' "$MCP_URL"
```

Sans token, la réponse doit être `401` :

```sh
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$MCP_URL"
```

Dans un terminal qui ne sera pas partagé, charger le token sans l’afficher :

```zsh
read -rs 'YAHOO_MCP_TOKEN?Remote MCP bearer token: '
printf '\n'
export YAHOO_MCP_TOKEN MCP_URL
.venv/bin/python scripts/smoke.py --url "$MCP_URL" --read-only --search
```

Le test doit découvrir les outils de lecture et d’analyse, puis afficher `Yahoo search OK`.

## 4. Utiliser le nettoyage distant

Après avoir validé le mode lecture seule, redéployer avec les mêmes paramètres en ajoutant `EnableRemoteWrites=true` à `--parameter-overrides`. La route navigateur utilise le jeton du lien, tandis que `/mcp` exige toujours le jeton Bearer.

Dans le client MCP authentifié :

1. Appeler `analyze_senders` pour repérer les listes très fréquentes ou volumineuses.
2. Appeler `unsubscribe_options` pour afficher uniquement les options standard `List-Unsubscribe`.
3. Appeler `suggest_cleanup` pour obtenir des candidats ; cette étape ne modifie rien.
4. Sélectionner explicitement les UID à déplacer et appeler `preview_cleanup`.
5. Ouvrir l’`approval_url` retournée dans un navigateur, vérifier les sujets et expéditeurs, puis cliquer sur **Approve move to Trash**.

L’approbation est consommée avant de contacter Yahoo. En cas d’erreur ou de réponse incertaine, vérifie dans Yahoo Mail avant de créer un nouvel aperçu. Le déplacement est alors effectué par Lambda via IMAP `MOVE`. Il n’y a pas de suppression permanente. Si Yahoo n’annonce pas `MOVE`, l’interface indiquera d’effectuer l’opération dans Yahoo Mail.

Ne pas demander à l’IA de suivre automatiquement l’URL d’approbation. Le clic doit être une action humaine après vérification de la preview.

## 5. Configuration d’un client MCP

Le endpoint distant utilise un bearer token préconfiguré. Exemple conceptuel :

```toml
[mcp_servers.yahoo-mail-aws]
url = "https://YOUR_API_ID.execute-api.eu-west-2.amazonaws.com/mcp"
bearer_token_env_var = "YAHOO_MCP_TOKEN"
```

Ce mécanisme est une authentification par secret partagé, pas un flux OAuth complet. Pour une authentification OAuth d’entreprise, placer Cognito ou un autre fournisseur OAuth 2.0 devant API Gateway et remplacer la validation bearer applicative par une validation JWT/API Gateway Authorizer.

## Dépannage et suppression

Consulter les logs uniquement pour les erreurs de démarrage, d’IMAP ou d’AWS :

```sh
sam logs --stack-name yahoo-mail-mcp --region eu-west-2 --tail
```

Vérifier aussi que le secret existe dans la même région, que son ARN est correct et que Yahoo accepte l’app password.

Pour supprimer le stack :

```sh
sam delete --stack-name yahoo-mail-mcp --region eu-west-2
```

Le secret créé séparément doit être supprimé ou conservé séparément : le stack ne le supprime pas.
