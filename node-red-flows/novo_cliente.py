#!/usr/bin/env python3
"""
Provisiona um cliente novo no SCADA-LTS compartilhado (Hetzner).
Uso: novo_cliente.py <nome_cliente> <cor_tema_hex>
Ex.:  novo_cliente.py clientedemo "#2f6d4f"

Porta e escolhida automaticamente (proxima livre a partir de 8082).

GUARD-RAIL DE RAM: a VM Hetzner ja teve 1 OOM real rodando Tomcats de
teste demais ao mesmo tempo (cada instancia SCADA-LTS usa ~350-500MB).
Durante iteracao/teste, NUNCA deixe mais de 1 cliente de teste ativo
por vez -- apague o anterior (docker rm + drop database + rm -rf
clients/<nome>) antes de criar o proximo.
"""
import subprocess
import sys
import secrets
import string
from pathlib import Path

STACK_DIR = Path("/opt/scadalts/stack")
TEMPLATE_DIR = STACK_DIR / "_template"
COMPOSE_FILE = STACK_DIR / "docker-compose.yml"


def sh(cmd, check=True):
    print(f"$ {cmd}")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        if check:
            raise RuntimeError(f"comando falhou: {cmd}")
    return r


def gen_password(n=20):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def proxima_porta_livre():
    import yaml
    with open(COMPOSE_FILE) as f:
        compose = yaml.safe_load(f)
    usadas = set()
    for svc in compose["services"].values():
        for p in svc.get("ports", []):
            # formato "127.0.0.1:8083:8080"
            partes = p.split(":")
            if len(partes) >= 2:
                usadas.add(int(partes[-2]))
    porta = 8082
    while porta in usadas:
        porta += 1
    return porta


def main():
    if len(sys.argv) != 4:
        print("Uso: novo_cliente.py <nome_cliente> <cor_tema_hex> <visitante_user> <visitante_pass>")
        sys.exit(1)

    nome = sys.argv[1].strip().lower()
    cor = sys.argv[2].strip()
    visitante_user = sys.argv[3] if len(sys.argv) > 3 else "Visitante"
    cor_hover = cor

    if not nome.isalnum():
        print("nome_cliente deve ser só letras/números, sem espaço/traço")
        sys.exit(1)

    porta = proxima_porta_livre()
    db_nome = f"scadalts_{nome}"
    db_user = f"scadalts_{nome}"
    db_senha = gen_password()
    client_dir = STACK_DIR / "clients" / nome

    if client_dir.exists():
        print(f"ERRO: {client_dir} já existe, abortando (não sobrescrevo cliente existente)")
        sys.exit(1)

    print(f"=== Provisionando cliente '{nome}' na porta {porta} ===")

    # 1. banco + usuario MySQL dedicado (mesmo padrao usado por qualquer cliente)
    sh(f"docker exec mysql mysql -u root -p__MYSQL_ROOT_PASSWORD__ -e "
       f"\"CREATE DATABASE IF NOT EXISTS {db_nome};\"")
    sh(f"docker exec mysql mysql -u root -p__MYSQL_ROOT_PASSWORD__ -e "
       f"\"CREATE USER IF NOT EXISTS '{db_user}'@'%' IDENTIFIED BY '{db_senha}';\"")
    sh(f"docker exec mysql mysql -u root -p__MYSQL_ROOT_PASSWORD__ -e "
       f"\"GRANT ALL PRIVILEGES ON {db_nome}.* TO '{db_user}'@'%';\"")
    sh(f"docker exec mysql mysql -u root -p__MYSQL_ROOT_PASSWORD__ -e \"FLUSH PRIVILEGES;\"")

    # 2. pastas
    (client_dir / "login").mkdir(parents=True)
    (client_dir / "graphics").mkdir(parents=True)
    (client_dir / "tomcat_log").mkdir(parents=True)

    logo_filename = f"{nome}-logo.png"
    logo_full_filename = f"{nome}-logo-full.png"

    subs = {
        "{{NOME_CLIENTE}}": nome,
        "{{CLIENTE_NOME}}": nome.capitalize(),
        "{{DB_SENHA}}": db_senha,
        "{{COR_TEMA}}": cor,
        "{{COR_TEMA_HOVER}}": cor_hover,
        "{{LOGO_FILENAME}}": logo_filename,
    }

    def render(src_name, dest_path):
        text = (TEMPLATE_DIR / src_name).read_text(encoding="utf-8")
        for k, v in subs.items():
            text = text.replace(k, v)
        dest_path.write_text(text, encoding="utf-8")

    render("context.xml", client_dir / "context.xml")
    (client_dir / "env.properties").write_text(
        (TEMPLATE_DIR / "env.properties").read_text(encoding="utf-8"), encoding="utf-8"
    )
    render("login.jsp", client_dir / "login" / "login.jsp")
    render("login-theme.css", client_dir / "login" / "login-theme.css")
    render("home.html", client_dir / "graphics" / "home.html")

    # logo: usa um placeholder generico ate o upload real do cliente ser aplicado
    placeholder_logo = TEMPLATE_DIR / "logo-placeholder.png"
    if placeholder_logo.exists():
        (client_dir / "login" / logo_filename).write_bytes(placeholder_logo.read_bytes())
        (client_dir / "graphics" / logo_full_filename).write_bytes(placeholder_logo.read_bytes())
        home_path = client_dir / "graphics" / "home.html"
        home_path.write_text(
            home_path.read_text(encoding="utf-8").replace(logo_filename, logo_full_filename),
            encoding="utf-8",
        )

    # 3. adiciona servico no docker-compose.yml (via yaml, preserva o resto)
    import yaml
    with open(COMPOSE_FILE) as f:
        compose = yaml.safe_load(f)

    service_name = f"scadalts-{nome}"
    compose["services"][service_name] = {
        "image": "scadalts/scadalts:v2.7.8.1",
        "restart": "unless-stopped",
        "environment": [
            "CATALINA_OPTS=-Xmx384m -Xms192m",
            "TZ=America/Sao_Paulo",
        ],
        "ports": [f"__TAILSCALE_IP_DA_VM__:{porta}:8080"],
        "depends_on": ["database"],
        "volumes": [
            f"./clients/{nome}/tomcat_log:/usr/local/tomcat/logs:rw",
            f"./clients/{nome}/context.xml:/usr/local/tomcat/webapps/Scada-LTS/META-INF/context.xml:ro",
            f"./clients/{nome}/env.properties:/usr/local/tomcat/webapps/Scada-LTS/WEB-INF/classes/env.properties:ro",
            f"./clients/{nome}/graphics:/usr/local/tomcat/webapps/Scada-LTS/graphics:rw",
            f"./clients/{nome}/login/login-theme.css:/usr/local/tomcat/webapps/Scada-LTS/assets/login-theme.css:ro",
            f"./clients/{nome}/login/{logo_filename}:/usr/local/tomcat/webapps/Scada-LTS/assets/{logo_filename}:ro",
            f"./clients/{nome}/login/login.jsp:/usr/local/tomcat/webapps/Scada-LTS/WEB-INF/jsp/login.jsp:ro",
        ],
        "links": ["database:database"],
        "command": [
            "/usr/bin/wait-for-it",
            "--host=database",
            "--port=3306",
            "--timeout=60",
            "--strict",
            "--",
            "/usr/local/tomcat/bin/catalina.sh",
            "run",
        ],
    }

    backup_path = COMPOSE_FILE.with_suffix(f".yml.bak.novocliente-{nome}")
    backup_path.write_text(COMPOSE_FILE.read_text())

    with open(COMPOSE_FILE, "w") as f:
        yaml.safe_dump(compose, f, default_flow_style=False, sort_keys=False)

    # 4. sobe o container novo
    sh(f"cd {STACK_DIR} && docker compose up -d {service_name}")

    print(f"\n=== OK: cliente '{nome}' provisionado ===")
    print(f"PORTA={porta}")
    print(f"db: {db_nome} / usuario mysql: {db_user} / senha: {db_senha}")
    print(f"acesso: http://<tailscale-ip>:{porta}/Scada-LTS/login.htm")


if __name__ == "__main__":
    main()
