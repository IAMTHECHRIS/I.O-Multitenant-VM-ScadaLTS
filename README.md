# Guia de Deploy — Supervisório SCADA-LTS multi-cliente (do zero)

Passo a passo genérico pra alguém de fora replicar toda a infraestrutura:
criar a VM, abrir as portas certas, baixar/subir os serviços, e deixar a
automação de cliente novo funcionando. Sem nome de cliente real — troque
`<nome-do-cliente>` pelo que fizer sentido no seu caso.

Este é um guia standalone, direto ao ponto — sem histórico de bugs
ou decisões de arquitetura, só o caminho pra ter tudo rodando do zero.

## 1. O que você vai ter no final

- Uma VM Linux rodando **MySQL** (um banco por cliente) + **N
  instâncias de SCADA-LTS** (uma por cliente, cada uma seu próprio
  container Docker) + **Node-RED** (automação de criar/excluir cliente
  e publicar domínio).
- Cada cliente acessível por dois caminhos: um IP interno (Tailscale,
  pra você administrar) e opcionalmente um domínio público
  (`<cliente>.seudominio.com`, via Cloudflare Tunnel — sem porta
  aberta pra internet).
- Um formulário web (Node-RED Dashboard) onde você digita nome/cor/login
  do cliente e ele sobe tudo sozinho em 2-4 minutos.

## 2. Provisionar a VM

Qualquer provedor cloud serve; este projeto usa Hetzner Cloud (bom
custo-benefício, datacenter Europa/EUA).

- **Specs mínimas**: 2 vCPU, 4GB RAM pra poucos clientes (~3-5). Cada
  instância SCADA-LTS usa uns 350-500MB de RAM; planeje
  `500MB × número de clientes simultâneos + 1GB pro MySQL/Node-RED/SO`.
- **SO**: Ubuntu 24.04 LTS.
- Anote o IP público — vai precisar pra SSH e pro Cloudflare Tunnel.

## 3. Portas — o que precisa abrir de verdade

**Resposta curta: nenhuma porta de aplicação precisa ficar aberta pra
internet.** O acesso público é só via Cloudflare Tunnel (conexão de
saída da VM pro Cloudflare, não entrada).

| Porta | Serviço | Exposição |
|---|---|---|
| 22 | SSH | Só seu IP, ou melhor: só via Tailscale |
| 3306 | MySQL | Nunca exposta — só rede Docker interna |
| 8080+ (uma por cliente) | SCADA-LTS (Tomcat) | Só no IP Tailscale da VM (rede mesh privada) |
| 1880 | Node-RED (dashboard de automação) | Só no IP Tailscale da VM |
| — | Cloudflare Tunnel | Sem porta — conexão de saída (outbound) da VM pro Cloudflare |
| 6000-6100 (se usar ABS Cel) | Bridge Modbus do Gateway | Só rede Docker interna, entre Gateway e SCADA-LTS |

Instale o **Tailscale** na VM (`curl -fsSL https://tailscale.com/install.sh | sh`,
depois `tailscale up`) — é assim que você acessa tudo sem abrir porta
nenhuma pro mundo.

## 4. Instalar Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

## 5. Subir o MySQL (banco compartilhado)

Cria `/opt/scadalts/stack/docker-compose.yml`:

```yaml
services:
  database:
    container_name: mysql
    image: mysql/mysql-server:8.0.32
    restart: unless-stopped
    environment:
      - MYSQL_ROOT_PASSWORD=<senha-forte-aqui>
      - MYSQL_USER=scadalts
      - MYSQL_PASSWORD=<senha-forte-aqui>
      - MYSQL_DATABASE=scadalts
    volumes:
      - ./db_data:/var/lib/mysql:rw
    command: --log_bin_trust_function_creators=1
```

```bash
cd /opt/scadalts/stack && docker compose up -d
```

## 6. Imagem do SCADA-LTS — qual baixar

**Não precisa baixar `.jar` manualmente** — a imagem Docker
`scadalts/scadalts` já vem com o SCADA-LTS (Tomcat + WAR) pronto.

```bash
docker pull scadalts/scadalts:v2.7.8.1
```

**Importante: fixe a versão, não use `:latest`.** No momento em que
este guia foi escrito, `:latest` resolve pra `v2.8.0`, que a própria
equipe do SCADA-LTS marca como **prerelease** no GitHub — não é a
versão estável recomendada pra produção. Confira a versão atual em
[github.com/SCADA-LTS/Scada-LTS/releases](https://github.com/SCADA-LTS/Scada-LTS/releases)
antes de fixar.

## 7. ABS Gateway/Master (só se usar modem ABS Cel)

Se o cliente tiver um modem celular ABS Cel fazendo a ponte Modbus, você
precisa das imagens proprietárias da ABS Telemetria (`abs-gateway`,
`abs-master`) — peça acesso ao registry deles, não são públicas. Sem
modem ABS, pule esta etapa (o cliente pode ter outro tipo de fonte de
dado, configurado como Data Source diferente dentro do próprio
SCADA-LTS).

## 8. Cloudflare Tunnel

1. No painel Cloudflare Zero Trust, crie um túnel novo, anote o `tunnel
   id` e baixe o arquivo de credencial (`.json`).
2. Instale o `cloudflared` na VM:
   ```bash
   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/bin/cloudflared
   chmod +x /usr/bin/cloudflared
   ```
3. `/etc/cloudflared/config.yml`:
   ```yaml
   tunnel: <seu-tunnel-id>
   credentials-file: /etc/cloudflared/<seu-tunnel-id>.json
   ingress:
     - service: http_status:404
   ```
4. Systemd service pra rodar sempre:
   ```bash
   cloudflared service install
   systemctl enable --now cloudflared
   ```

**Watcher de reload automático** — como cada cliente novo precisa de
uma linha nova em `ingress:`, e reiniciar o `cloudflared` manualmente
toda vez é chato/arriscado de esquecer, crie um watcher systemd que
reinicia sozinho quando o arquivo muda (arquivos prontos em
`systemd/cloudflared-reload.path` e `systemd/cloudflared-reload.service`
deste repo):

```bash
cp systemd/cloudflared-reload.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cloudflared-reload.path
```

## 9. Node-RED (automação de cliente novo)

```bash
mkdir -p /opt/node-red/data
```

`/opt/node-red/docker-compose.yml`:

```yaml
services:
  node-red:
    build: .
    image: node-red-com-docker
    container_name: node-red
    restart: unless-stopped
    user: root
    ports:
      - "<IP_TAILSCALE_DA_VM>:1880:1880"
    volumes:
      - ./data:/data
      - /var/run/docker.sock:/var/run/docker.sock
      - /opt/scadalts/stack:/opt/scadalts/stack
      - /etc/cloudflared/config.yml:/etc/cloudflared/config.yml
      - /opt/_backups_pre_reorg:/opt/_backups_pre_reorg
    networks:
      - default
      - scadalts_net
networks:
  default: {}
  scadalts_net:
    external: true
    name: stack_default
```

`Dockerfile` ao lado (a imagem base do Node-RED não vem com Docker CLI
nem PyYAML, e o fluxo de automação precisa dos dois):

```dockerfile
FROM nodered/node-red:latest
USER root
RUN apk add --no-cache docker-cli docker-cli-compose py3-yaml
USER node-red
```

```bash
cd /opt/node-red && docker compose up -d
```

Monte o fluxo Node-RED (formulário de criar cliente + barra de
progresso + botão de publicar domínio + botão de excluir com senha
mestra) no editor visual (`http://<IP_TAILSCALE_DA_VM>:1880`) — a
lógica é: formulário → roda o script de provisionamento → aguarda o
Tomcat subir → cria login admin+cliente via API REST do SCADA-LTS →
mostra resultado. Reinicie o container depois de qualquer mudança.

**Token Cloudflare pro botão "Publicar domínio"**: crie em
[dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens),
permissões `Zone → DNS → Edit` + `Account → Cloudflare Tunnel → Edit`,
escopo só na sua zona/domínio.

Deixe um script de provisionamento em
`/opt/scadalts/stack/scripts/novo_cliente.py` que: cria banco+usuário
MySQL pro cliente, escolhe a próxima porta livre, copia os templates
de login/tema (`/opt/scadalts/stack/_template/`) substituindo
placeholders de nome/cor, adiciona o serviço no `docker-compose.yml`, e
sobe o container.

## 10. Criar o primeiro cliente

Com tudo no ar: abra o dashboard Node-RED, aba "Cliente Novo", preencha
nome/cor/login, clique em Criar. Acompanhe a barra de progresso (leva
uns 2-4 minutos, a maior parte é o Tomcat subindo). No fim, o cliente
aparece na aba "Clientes Ativos" com link de acesso — daí é só clicar
em "Publicar domínio" se quiser um link público.

## Resumo dos "arquivos a baixar"

| O quê | De onde | Precisa de conta/token? |
|---|---|---|
| Imagem SCADA-LTS | `docker pull scadalts/scadalts:v2.7.8.1` | Não |
| Imagem MySQL | `docker pull mysql/mysql-server:8.0.32` | Não |
| `cloudflared` | GitHub releases da Cloudflare | Não (mas precisa de conta Cloudflare pra criar o túnel) |
| Imagens ABS Gateway/Master | Registry privado da ABS Telemetria | Sim, só se usar modem ABS Cel |
| Templates de login/tema (JSP + CSS) | Vem junto com o próprio SCADA-LTS, customize a partir do padrão dele | Não |
