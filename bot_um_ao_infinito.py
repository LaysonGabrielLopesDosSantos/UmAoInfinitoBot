"""
bot de discord - um ao infinito 6.0 (v2)
prefixo: !
banco de dados: sqlite local (fichas.db, ao lado deste script)

instalacao:
    python3 -m venv venv
    source venv/bin/activate
    pip install -U discord.py

token:
    export DISCORD_TOKEN="seu_token"   (ou arquivo token.txt ao lado deste script)

executar:
    python bot_um_ao_infinito.py

se o fichas.db for de uma versao anterior, ele e migrado automaticamente e uma
copia de seguranca e salva na pasta backups/

novidades desta versao:
- !transferir: passa itens; !pix: transfere moedas entre fichas
- moedas + loja: !saldo, !pix, !loja, !comprar, !vender (so vende o que foi comprado ou achado), !darmoedas (admin)
- habilidades da loja sao configuradas em loja_habilidades.json
- ataques com `int` causam dano fisico
- dungeons sem restricao de nivel: qualquer ficha pode entrar em qualquer dificuldade
- eventos com pequenas decisoes no acampamento (baus, armadilhas, altares...): !escolher
- ranking de abates por dungeon com mvp (!dungeon ranking) e ranking do servidor (!ranking)
"""

import asyncio
import datetime
import json
import os
import random
import re
import shutil
import sqlite3
import time
import traceback
import unicodedata

import discord
from discord.ext import commands, tasks

# configuracoes
PASTA = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PASTA, "fichas.db")
PASTA_BACKUPS = os.path.join(PASTA, "backups")
BACKUPS_GUARDADOS = 7          # backups diarios mantidos
VALIDADE_LIXEIRA = 30 * 60  # segundos que uma exclusao pode ser desfeita com !restaurar
MAX_FICHAS = 2  # limite padrao de fichas por pessoa (admin pode mudar)
LIMITE_ATRIBUTO = 20  # pontos maximos por atributo
ASPAS = '"“”'

# chaves internas -> siglas
ATRIBUTOS = {
    "forca": "for", "resistencia": "res", "agilidade": "agi", "combate": "com",
    "inteligencia": "int", "sabedoria": "sab", "carisma": "car", "precisao": "pre",
}
SIGLAS = {v: k for k, v in ATRIBUTOS.items()}
SIG = {**ATRIBUTOS, "corpo": "crp", "mente": "mnt"}
VITAIS = {"corpo": "corpo", "crp": "corpo", "mente": "mente", "mnt": "mente"}
# atributos afetados pela penalidade de mente
MENTAIS = {"combate", "inteligencia", "sabedoria", "carisma", "precisao"}
TIPOS_ITEM = {
    "normal": "estatico",
    "estatico": "estatico",
    "consumivel": "consumivel",
}
TIPOS_HABILIDADE = {
    "perto": "perto",
    "longe": "longe",
    "magia": "magia",
    "jujutsu": "jujutsu",
    "melhoria": "buff",
    "mel": "buff",
    "buff": "buff",
    "penalidade": "debuff",
    "pen": "debuff",
    "debuff": "debuff",
}
MODOS_ATAQUE = {
    "perto": ("combate", "forca"),
    "longe": ("precisao", ("forca", ("inteligencia", 2))),
    "magia": ("precisao", "inteligencia"),
    "jujutsu": ("combate", ("forca", ("inteligencia", 2))),
}


def nome_tipo_item(tipo: str) -> str:
    return {"estatico": "normal", "consumivel": "consumível"}.get(tipo, tipo)


def nome_tipo_habilidade(modo: str) -> str:
    return {"buff": "melhoria (mel)", "debuff": "penalidade (pen)"}.get(modo, modo)


# banco de dados (com migracao automatica)
def precisa_migrar() -> bool:
    if not os.path.exists(DB_PATH):
        return False
    c = sqlite3.connect(DB_PATH)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(fichas)")]
        if cols and ("xp" in cols or "ordem" not in cols or "moedas" not in cols):
            return True
        tem_status = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='status'").fetchone()
        if tem_status and c.execute("SELECT 1 FROM status WHERE tipo IN ('static', 'dynamic') LIMIT 1").fetchone():
            return True
        tem_hab = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='habilidades'").fetchone()
        if tem_hab and "ativa" not in [r[1] for r in c.execute("PRAGMA table_info(habilidades)")]:
            return True
        return False
    finally:
        c.close()


if precisa_migrar():
    os.makedirs(PASTA_BACKUPS, exist_ok=True)
    _destino = os.path.join(PASTA_BACKUPS, f"fichas-antes-da-migracao-{datetime.datetime.now():%Y%m%d-%H%M%S}.db")
    shutil.copy2(DB_PATH, _destino)
    print(f"copia de seguranca salva em {_destino}")

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS fichas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    dono_id INTEGER NOT NULL,
    nome TEXT NOT NULL COLLATE NOCASE,
    nivel INTEGER NOT NULL DEFAULT 1,
    ordem INTEGER NOT NULL DEFAULT 0,
    forca INTEGER NOT NULL DEFAULT 0,
    resistencia INTEGER NOT NULL DEFAULT 0,
    agilidade INTEGER NOT NULL DEFAULT 0,
    combate INTEGER NOT NULL DEFAULT 0,
    inteligencia INTEGER NOT NULL DEFAULT 0,
    sabedoria INTEGER NOT NULL DEFAULT 0,
    carisma INTEGER NOT NULL DEFAULT 0,
    precisao INTEGER NOT NULL DEFAULT 0,
    corpo INTEGER NOT NULL DEFAULT 60,
    mente INTEGER NOT NULL DEFAULT 60,
    UNIQUE (guild_id, nome)
);
CREATE TABLE IF NOT EXISTS itens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ficha_id INTEGER NOT NULL REFERENCES fichas(id) ON DELETE CASCADE,
    nome TEXT NOT NULL COLLATE NOCASE,
    quantidade INTEGER NOT NULL DEFAULT 1,
    bonus TEXT NOT NULL DEFAULT '{}',
    equipado INTEGER NOT NULL DEFAULT 0,
    tipo TEXT NOT NULL DEFAULT 'estatico',
    UNIQUE (ficha_id, nome)
);
CREATE TABLE IF NOT EXISTS status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ficha_id INTEGER NOT NULL REFERENCES fichas(id) ON DELETE CASCADE,
    nome TEXT NOT NULL COLLATE NOCASE,
    tipo TEXT NOT NULL,
    bonus TEXT NOT NULL DEFAULT '{}',
    duracao INTEGER,
    UNIQUE (ficha_id, nome)
);
CREATE TABLE IF NOT EXISTS notas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ficha_id INTEGER NOT NULL REFERENCES fichas(id) ON DELETE CASCADE,
    titulo TEXT NOT NULL COLLATE NOCASE,
    texto TEXT NOT NULL,
    UNIQUE (ficha_id, titulo)
);
CREATE TABLE IF NOT EXISTS habilidades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ficha_id INTEGER NOT NULL REFERENCES fichas(id) ON DELETE CASCADE,
    nome TEXT NOT NULL COLLATE NOCASE,
    bonus TEXT NOT NULL DEFAULT '{}',
    duracao INTEGER,
    ativa INTEGER NOT NULL DEFAULT 0,
    restam INTEGER,
    modo TEXT NOT NULL DEFAULT 'buff',
    UNIQUE (ficha_id, nome)
);
CREATE TABLE IF NOT EXISTS lixeira (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    tipo TEXT NOT NULL,
    descricao TEXT NOT NULL,
    dados TEXT NOT NULL,
    criado_em INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS historico (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ficha_id INTEGER NOT NULL REFERENCES fichas(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    texto TEXT NOT NULL,
    criado_em INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
    guild_id INTEGER PRIMARY KEY,
    limite_padrao INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS limites (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    limite INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS ranking (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    abates INTEGER NOT NULL DEFAULT 0,
    dano INTEGER NOT NULL DEFAULT 0,
    mvps INTEGER NOT NULL DEFAULT 0,
    dungeons INTEGER NOT NULL DEFAULT 0,
    vitorias INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
"""


def colunas_tabela(tabela: str):
    return [r["name"] for r in db.execute(f"PRAGMA table_info({tabela})")]


def garantir_coluna(tabela, coluna, definicao):
    if coluna not in colunas_tabela(tabela):
        db.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


def remover_coluna_xp():
    """o xp foi removido do bot; tira a coluna dos bancos antigos (ha backup antes da migracao)."""
    if "xp" in colunas_tabela("fichas"):
        try:
            db.execute("ALTER TABLE fichas DROP COLUMN xp")
            db.commit()
        except sqlite3.OperationalError as erro:
            print("nao foi possivel remover a coluna xp (ela fica sem uso):", erro)


def preparar_banco():
    """centraliza a inicializacao do banco, as migracoes e as colunas extras do SQLite."""
    db.executescript(DB_SCHEMA)
    remover_coluna_xp()
    # bancos antigos: a habilidade ativa era guardada como status; passa para a propria habilidade (veja mais abaixo)
    global MIGRAR_HAB_ATIVAS
    MIGRAR_HAB_ATIVAS = "ativa" not in colunas_tabela("habilidades")

    for tabela, coluna, definicao in (
        ("habilidades", "ativa", "INTEGER NOT NULL DEFAULT 0"),
        ("habilidades", "restam", "INTEGER"),
        ("habilidades", "modo", "TEXT NOT NULL DEFAULT 'buff'"),
        ("itens", "tipo", "TEXT NOT NULL DEFAULT 'estatico'"),
        ("fichas", "ordem", "INTEGER NOT NULL DEFAULT 0"),
        ("fichas", "exp", "INTEGER NOT NULL DEFAULT 0"),
        ("fichas", "moedas", "INTEGER NOT NULL DEFAULT 0"),
        ("itens", "vendaveis", "INTEGER NOT NULL DEFAULT 0"),
        ("habilidades", "mental", "INTEGER NOT NULL DEFAULT 0"),
        ("habilidades", "requisitos", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        garantir_coluna(tabela, coluna, definicao)

    db.execute("UPDATE status SET tipo='estatico' WHERE tipo='static'")
    db.execute("UPDATE status SET tipo='dinamico' WHERE tipo='dynamic'")
    db.commit()


preparar_banco()


# utilidades gerais
def fazer_backup():
    """uma copia do banco por dia, na pasta backups/; guarda as mais recentes."""
    os.makedirs(PASTA_BACKUPS, exist_ok=True)
    destino = os.path.join(PASTA_BACKUPS, f"fichas-{datetime.date.today().isoformat()}.db")
    if not os.path.exists(destino):
        copia = sqlite3.connect(destino)
        try:
            db.backup(copia)
        finally:
            copia.close()
    todos = sorted(n for n in os.listdir(PASTA_BACKUPS) if re.fullmatch(r"fichas-\d{4}-\d{2}-\d{2}\.db", n))
    for nome in todos[:-BACKUPS_GUARDADOS]:
        os.remove(os.path.join(PASTA_BACKUPS, nome))


def norm(texto: str) -> str:
    """minusculas e sem acentos."""
    t = unicodedata.normalize("NFD", texto.lower().strip())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def achar_attr(texto: str):
    t = norm(texto)
    if t in ATRIBUTOS:
        return t
    return SIGLAS.get(t)


def tokenizar(raw: str):
    """divide em palavras respeitando aspas. retorna [(texto, posicao_inicial)]."""
    tokens, i, n = [], 0, len(raw)
    while i < n:
        while i < n and raw[i].isspace():
            i += 1
        if i >= n:
            break
        inicio = i
        if raw[i] in ASPAS:
            i += 1
            buf = []
            while i < n and raw[i] not in ASPAS:
                buf.append(raw[i])
                i += 1
            i += 1
            tokens.append(("".join(buf), inicio))
        else:
            while i < n and not raw[i].isspace():
                i += 1
            tokens.append((raw[inicio:i], inicio))
    return tokens


def parse_valor(txt: str):
    """'5' -> (None, 5); '+2' -> ('+', 2); '-1' -> ('-', 1)."""
    m = re.fullmatch(r"([+-])?(\d+)", txt.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def aplicar_vital(atual: int, pv, maximo: int) -> int:
    """pv: (sinal, n) numerico, ou ("%", sinal, n) = n% do maximo (sem sinal: define o valor)."""
    if pv[0] == "%":
        _, sinal, n = pv
        quant = n * maximo // 100
        return atual + quant if sinal == "+" else atual - quant if sinal == "-" else quant
    return aplicar_valor(atual, pv)


def aplicar_valor(atual: int, pv) -> int:
    sinal, n = pv
    if sinal == "+":
        return atual + n
    if sinal == "-":
        return atual - n
    return n


def fmt_nivel(n: int) -> str:
    """nivel -1 = ficha livre (ignora os pontos padrao por nivel)."""
    return "livre" if n == -1 else str(n)


def compacto(texto: str) -> str:
    return norm(texto).replace(" ", "")


def candidatos_nome(rows, nome: str, chave: str = "nome", substring: bool = True):
    """acha nomes pelo texto digitado, em camadas: igual, igual sem espacos, comeca com, contem."""
    alvo, alvo_c = norm(nome), compacto(nome)
    if not alvo_c:
        return []
    camadas = [
        lambda r: norm(r[chave]) == alvo,
        lambda r: compacto(r[chave]) == alvo_c,
        lambda r: compacto(r[chave]).startswith(alvo_c),
    ]
    if substring:
        camadas.append(lambda r: alvo_c in compacto(r[chave]))
    for cond in camadas:
        achados = [r for r in rows if cond(r)]
        if achados:
            return achados
    return []


def achar_nome(rows, nome: str, chave: str = "nome", exato: bool = False, substring: bool = True,
               seguro: bool = False):
    """ignora maiusculas e acentos. exato=True: so nome igual. senao aceita o comeco do nome
    (autocompletar); se mais de um nome combinar, nao escolhe nenhum.
    seguro=True (comandos que apagam): so nome igual ou comeco do nome com pelo menos 3 letras."""
    alvo = norm(nome)
    for r in rows:
        if norm(r[chave]) == alvo:
            return r
    if exato:
        return None
    if seguro:
        if len(compacto(nome)) < 3:
            return None
        substring = False
    achados = candidatos_nome(rows, nome, chave, substring)
    return achados[0] if len(achados) == 1 else None


async def enviar_longo(ctx, texto: str):
    limite = 1800
    paginas, atual = [], ""
    for linha in texto.splitlines():
        while len(linha) > limite:
            corte = linha.rfind(" ", 0, limite + 1)
            if corte <= 0:
                corte = limite
            parte, linha = linha[:corte], linha[corte:].lstrip()
            if atual:
                paginas.append(atual)
                atual = ""
            paginas.append(parte)
        candidato = f"{atual}\n{linha}" if atual else linha
        if len(candidato) > limite:
            paginas.append(atual)
            atual = linha
        else:
            atual = candidato
    if atual or not paginas:
        paginas.append(atual)

    total = len(paginas)
    for indice, pagina in enumerate(paginas, 1):
        conteudo = pagina if total == 1 else f"📄 **Página {indice}/{total}**\n{pagina}"
        await ctx.send(conteudo)


def eh_admin(ctx) -> bool:
    return ctx.author.guild_permissions.administrator


def pode_editar(ctx, ficha) -> bool:
    return ficha["dono_id"] == ctx.author.id or eh_admin(ctx)


async def checar_edicao(ctx, ficha) -> bool:
    if pode_editar(ctx, ficha):
        return True
    await ctx.send("🚫 voce so pode alterar a sua propria ficha.")
    return False


# fichas: acesso e calculos
def get_ficha(guild_id, nome, exato=False, substring=True, seguro=False):
    rows = db.execute("SELECT * FROM fichas WHERE guild_id=?", (guild_id,)).fetchall()
    return achar_nome(rows, nome, exato=exato, substring=substring, seguro=seguro)


def candidatos_ficha(guild_id, nome, substring=True):
    rows = db.execute("SELECT * FROM fichas WHERE guild_id=?", (guild_id,)).fetchall()
    return candidatos_nome(rows, nome, substring=substring)


def get_ficha_id(ficha_id):
    return db.execute("SELECT * FROM fichas WHERE id=?", (ficha_id,)).fetchone()


def fichas_do_usuario(guild_id, user_id):
    return db.execute(
        "SELECT * FROM fichas WHERE guild_id=? AND dono_id=? ORDER BY ordem, id", (guild_id, user_id)
    ).fetchall()


def renumerar(guild_id, dono_id):
    """deixa a ordem das fichas do dono como 1, 2, 3..."""
    rows = db.execute("SELECT id FROM fichas WHERE guild_id=? AND dono_id=? ORDER BY ordem, id",
                      (guild_id, dono_id)).fetchall()
    for i, r in enumerate(rows, 1):
        db.execute("UPDATE fichas SET ordem=? WHERE id=?", (i, r["id"]))
    db.commit()


def get_item(ficha_id, nome, exato=False, seguro=False):
    return achar_nome(db.execute("SELECT * FROM itens WHERE ficha_id=?", (ficha_id,)).fetchall(), nome,
                      exato=exato, seguro=seguro)


def get_status(ficha_id, nome, exato=False, seguro=False):
    return achar_nome(db.execute("SELECT * FROM status WHERE ficha_id=?", (ficha_id,)).fetchall(), nome,
                      exato=exato, seguro=seguro)


def get_nota(ficha_id, titulo, exato=False, seguro=False):
    return achar_nome(db.execute("SELECT * FROM notas WHERE ficha_id=?", (ficha_id,)).fetchall(), titulo, "titulo",
                      exato=exato, seguro=seguro)


def get_habilidade(ficha_id, nome, exato=False, seguro=False):
    return achar_nome(db.execute("SELECT * FROM habilidades WHERE ficha_id=?", (ficha_id,)).fetchall(), nome,
                      exato=exato, seguro=seguro)


def migrar_habilidades_ativas():
    """so roda uma vez, quando a coluna 'ativa' acabou de ser criada: o status estatico com o nome
    de uma habilidade (a forma antiga de 'ativa') vira a propria habilidade ativa."""
    for h in db.execute("SELECT * FROM habilidades").fetchall():
        st = achar_nome(db.execute("SELECT * FROM status WHERE ficha_id=? AND tipo='estatico'",
                                   (h["ficha_id"],)).fetchall(), h["nome"], exato=True)
        if st:
            db.execute("UPDATE habilidades SET ativa=1, restam=? WHERE id=?", (st["duracao"], h["id"]))
            db.execute("DELETE FROM status WHERE id=?", (st["id"],))
    db.commit()


if MIGRAR_HAB_ATIVAS:
    migrar_habilidades_ativas()


def msg_ficha_nao_achada(guild_id, nome) -> str:
    cands = candidatos_ficha(guild_id, nome)
    if len(cands) > 1:
        return "nome ambiguo: " + ", ".join(f"**{c['nome']}**" for c in cands[:8]) + ". escreva mais do nome."
    return f"nao encontrei a ficha **{nome}**."


TABELAS_NOME = {"item": ("itens", "nome", "o"), "status": ("status", "nome", "o"),
                "nota": ("notas", "titulo", "a"), "habilidade": ("habilidades", "nome", "a")}


def msg_nao_tem(f, rotulo, nome) -> str:
    tabela, chave, artigo = TABELAS_NOME[rotulo]
    rows = db.execute(f"SELECT * FROM {tabela} WHERE ficha_id=?", (f["id"],)).fetchall()
    cands = candidatos_nome(rows, nome, chave)
    if len(cands) > 1:
        return (f"nome ambiguo em **{f['nome']}**: " + ", ".join(f"**{c[chave]}**" for c in cands[:8])
                + ". escreva mais do nome.")
    return f"**{f['nome']}** nao tem {artigo} {rotulo} **{nome}**."


def limite_padrao(guild_id) -> int:
    r = db.execute("SELECT limite_padrao FROM config WHERE guild_id=?", (guild_id,)).fetchone()
    return r["limite_padrao"] if r else MAX_FICHAS


def limite_de(guild_id, user_id) -> int:
    r = db.execute("SELECT limite FROM limites WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
    return r["limite"] if r else limite_padrao(guild_id)


def valor_bonus(v, base: int) -> int:
    """v e um inteiro ou uma porcentagem em texto ('50%', '-10%'). porcentagem = N% de `base`;
    arredonda para baixo o valor absoluto e mantem o sinal."""
    if isinstance(v, str):
        pct = int(v[:-1])
        quant = abs(pct) * max(base, 0) // 100
        return quant if pct >= 0 else -quant
    return v


def fmt_valor(v) -> str:
    if isinstance(v, str):
        return v if v.startswith("-") else "+" + v.lstrip("+")
    return f"{v:+d}"


def get_bonus(ficha_id) -> dict:
    """soma bonus de itens estaticos equipados (por unidade equipada) e de status estatico.
    porcentagem em atributo vale N% do valor base do atributo (recalculado a cada uso)."""
    f = get_ficha_id(ficha_id)
    if not f:
        return {}
    total = {}
    for r in db.execute("SELECT bonus, equipado FROM itens WHERE ficha_id=? AND equipado>0 AND tipo='estatico'",
                        (ficha_id,)):
        for k, v in json.loads(r["bonus"]).items():
            total[k] = total.get(k, 0) + valor_bonus(v, f[k] if k in ATRIBUTOS else 0) * r["equipado"]
    for r in db.execute("SELECT bonus FROM status WHERE ficha_id=? AND tipo='estatico'", (ficha_id,)):
        for k, v in json.loads(r["bonus"]).items():
            total[k] = total.get(k, 0) + valor_bonus(v, f[k] if k in ATRIBUTOS else 0)
    for r in db.execute("SELECT bonus FROM habilidades WHERE ficha_id=? AND ativa=1", (ficha_id,)):
        for k, v in json.loads(r["bonus"]).items():
            if k in ATRIBUTOS:
                total[k] = total.get(k, 0) + valor_bonus(v, f[k])
    return total


def maximos(ficha, bonus):
    """vigor = 60 + res base, vontade = 60 + sab base (bonus de itens/status nao entram)."""
    vigor = 60 + ficha["resistencia"]
    vontade = 60 + ficha["sabedoria"]
    return vigor, vontade


def normalizar_vitais(ficha_id):
    f = get_ficha_id(ficha_id)
    vigor, vontade = maximos(f, get_bonus(ficha_id))
    corpo = max(0, min(f["corpo"], vigor))
    mente = max(0, min(f["mente"], vontade))
    db.execute("UPDATE fichas SET corpo=?, mente=? WHERE id=?", (corpo, mente, ficha_id))
    db.commit()


def penalidade(valor: int) -> int:
    if valor > 40:
        return 0
    if valor > 30:
        return 1
    if valor > 20:
        return 2
    if valor > 10:
        return 3
    return 4  # 10 a 0


def fmt_bonus(b: dict, por_turno=False) -> str:
    if not b:
        return "sem bonus"
    texto = ", ".join(f"{fmt_valor(v)} {SIG[k]}" for k, v in b.items())
    return texto + (" por turno" if por_turno else "")


def parse_bonus(attrs_txt: str, vals_txt: str, permitidos):
    """'+for,-agi' e '2,1' -> {'forca': 2, 'agilidade': -1}; valores aceitam % ('+crp 50%' -> {'corpo': '50%'})"""
    attrs = [a for a in attrs_txt.split(",") if a.strip()]
    vals = [v for v in vals_txt.split(",") if v.strip()]
    if not attrs or len(attrs) != len(vals):
        return None, "a quantidade de atributos e de valores precisa ser a mesma (separe por virgula, sem espacos)."
    saida = {}
    for a, v in zip(attrs, vals):
        a = a.strip()
        sinal = -1 if a.startswith("-") else 1
        nome = a.lstrip("+-").strip()
        chave = achar_attr(nome) or VITAIS.get(norm(nome))
        if chave is None or chave not in permitidos:
            return None, f"atributo invalido aqui: **{nome}**."
        v = v.strip()
        eh_pct = v.endswith("%")
        try:
            valor = int(v[:-1] if eh_pct else v)
        except ValueError:
            return None, f"valor invalido: **{v}**."
        novo = sinal * valor
        atual = saida.get(chave)
        if atual is None:
            saida[chave] = f"{novo}%" if eh_pct else novo
        elif isinstance(atual, str) != eh_pct:
            return None, f"**{SIG[chave]}** mistura valor fixo e porcentagem; use so um dos dois."
        elif eh_pct:
            saida[chave] = f"{int(atual[:-1]) + novo}%"
        else:
            saida[chave] = atual + novo
    return saida, None


def validar_nome(nome: str):
    if not nome:
        return "o nome nao pode ficar vazio."
    if len(nome) > 40:
        return "o nome deve ter ate 40 caracteres."
    return None


def cortar_lista(itens, limite=900) -> str:
    """junta nomes por virgula sem passar do limite de um campo do embed (1024)."""
    if not itens:
        return "—"
    texto = ""
    for i, it in enumerate(itens):
        novo = (", " if texto else "") + it
        if len(texto) + len(novo) > limite:
            return texto + f" … e mais {len(itens) - i}"
        texto += novo
    return texto


def limpar_lixeira():
    db.execute("DELETE FROM lixeira WHERE criado_em < ?", (int(time.time()) - VALIDADE_LIXEIRA,))
    db.commit()


def serializar_ficha(f) -> dict:
    """tudo que pertence a uma ficha, para poder restaurar depois."""
    d = {k: f[k] for k in f.keys() if k != "id"}
    fid = f["id"]
    d["itens"] = [{k: r[k] for k in ("nome", "quantidade", "bonus", "equipado", "tipo", "vendaveis")}
                  for r in db.execute("SELECT * FROM itens WHERE ficha_id=?", (fid,))]
    d["status"] = [{k: r[k] for k in ("nome", "tipo", "bonus", "duracao")}
                   for r in db.execute("SELECT * FROM status WHERE ficha_id=?", (fid,))]
    d["notas"] = [{k: r[k] for k in ("titulo", "texto")}
                  for r in db.execute("SELECT * FROM notas WHERE ficha_id=?", (fid,))]
    d["habilidades"] = [{k: r[k] for k in ("nome", "bonus", "duracao", "ativa", "restam", "modo", "mental", "requisitos")}
                        for r in db.execute("SELECT * FROM habilidades WHERE ficha_id=?", (fid,))]
    return d


def guardar_lixeira(ctx, tipo: str, descricao: str, dados):
    limpar_lixeira()
    db.execute("INSERT INTO lixeira (guild_id, user_id, tipo, descricao, dados, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
               (ctx.guild.id, ctx.author.id, tipo, descricao, json.dumps(dados), int(time.time())))
    db.commit()


def embed_ficha(f) -> discord.Embed:
    bonus = get_bonus(f["id"])
    vigor, vontade = maximos(f, bonus)
    emb = discord.Embed(
        title=f"📜 ficha · {f['nome']}",
        description=f"👤 jogador: <@{f['dono_id']}> • 🏷️ ordem #{f['ordem']} • 📊 nivel {fmt_nivel(f['nivel'])}",
        color=discord.Color.from_rgb(212, 175, 55),
    )
    emb.set_footer(text="RPGMASTER")
    linhas = []
    for k, sg in ATRIBUTOS.items():
        b = bonus.get(k, 0)
        extra = f" ({b:+d})" if b else ""
        linhas.append(f"**{sg}**: {f[k]}{extra}")
    emb.add_field(name="🧠 atributos", value="\n".join(linhas), inline=False)
    emb.add_field(name="❤️ corpo", value=f"{f['corpo']}/{vigor}", inline=True)
    emb.add_field(name="🧠 mente", value=f"{f['mente']}/{vontade}", inline=True)
    usados = sum(f[k] for k in ATRIBUTOS)
    limite_pontos = "sem limite" if f["nivel"] == -1 else str(4 * f["nivel"])
    emb.add_field(name="📈 pontos", value=f"{usados}/{limite_pontos}", inline=True)
    if f["nivel"] == -1:
        xp_txt = "—"
    elif f["nivel"] >= 20:
        xp_txt = "nivel maximo"
    else:
        xp_txt = f"{f['exp']}/{100 * f['nivel']}"
    emb.add_field(name="✨ xp", value=xp_txt, inline=True)
    emb.add_field(name="💰 moedas", value=f"{f['moedas']}", inline=True)
    eq = [f"{r['nome']} ({r['equipado']}/{r['quantidade']})" if r["quantidade"] > 1 else r["nome"]
          for r in db.execute("SELECT nome, equipado, quantidade FROM itens WHERE ficha_id=? AND equipado>0",
                              (f["id"],))]
    st = [r["nome"] for r in db.execute("SELECT nome FROM status WHERE ficha_id=?", (f["id"],))]
    emb.add_field(name="⚔️ equipados", value=cortar_lista(eq), inline=False)
    emb.add_field(name="🛡️ status", value=cortar_lista(st), inline=False)
    hs = [f"{'🟢' if r['ativa'] else '⚪'} {r['nome']}"
          for r in db.execute("SELECT nome, ativa FROM habilidades WHERE ficha_id=? ORDER BY nome",
                              (f["id"],))]
    emb.add_field(name="✨ habilidades", value=cortar_lista(hs), inline=False)
    return emb


def registrar_alvo(ctx, f):
    """marca a ficha como alvo do comando em andamento (usado pelo historico)."""
    alvos = getattr(ctx, "_alvos", None)
    if alvos is None:
        alvos = ctx._alvos = []
    if f["id"] not in alvos:
        alvos.append(f["id"])


PALAVRAS_RESERVADAS = {"corpo", "mente", "crp", "mnt", "vigor", "vgr", "vontade", "vtd", "nivel",
                       "normal", "estatico", "dinamico", "consumivel", "melhoria", "mel", "buff",
                       "penalidade", "pen", "debuff", "persistente", "livre", "padrao", "reset", "sim"}


def eh_reservada(txt: str) -> bool:
    """palavras que nunca sao tratadas como comeco do nome de uma ficha (siglas, tipos, numeros...)."""
    t = norm(txt)
    return (not t or achar_attr(t) is not None or t in PALAVRAS_RESERVADAS
            or t[0] in "+-#" or t.isdigit())


def aridade_ok(aridades, n: int) -> bool:
    if aridades is None:
        return False
    return aridades(n) if callable(aridades) else n in aridades


async def resolver(ctx, tokens, editar=False, numero_e_valor=False, sem_resto=False, aridades=None):
    """
    descobre de qual ficha o comando fala.
    - primeiro argumento = nome da ficha, a ordem das suas fichas (`#2`, ou so `2` quando nao se confunde
      com um valor numerico) ou o comeco do nome (autocompletar: `Kage` para `KageYaeger`). o autocompletar
      so vale se sobrarem argumentos suficientes para o comando (aridades).
    - sem nome nem ordem: usa a sua ficha numero 1.
    retorna (ficha, tokens_restantes) ou (None, None) depois de avisar o usuario.
    """
    gid = ctx.guild.id
    minhas = fichas_do_usuario(gid, ctx.author.id)
    ficha, resto = None, tokens
    if tokens:
        txt = tokens[0][0]
        f = get_ficha(gid, txt, exato=True)
        if f:
            ficha, resto = f, tokens[1:]
        else:
            m = re.fullmatch(r"#(\d+)", txt)
            if m is None and not numero_e_valor:
                m = re.fullmatch(r"(\d+)", txt)
            if m:
                n = int(m.group(1))
                if 1 <= n <= len(minhas):
                    ficha, resto = minhas[n - 1], tokens[1:]
                elif txt.startswith("#"):
                    await ctx.send(f"voce nao tem a ficha #{n} (voce tem {len(minhas)}). veja a ordem com `!ordem`.")
                    return None, None
            if ficha is None and not eh_reservada(txt) and aridade_ok(aridades, len(tokens) - 1):
                cands = candidatos_ficha(gid, txt, substring=False)
                if len(cands) == 1:
                    ficha, resto = cands[0], tokens[1:]
                elif len(cands) > 1:
                    nomes = ", ".join(f"**{c['nome']}**" for c in cands[:8])
                    await ctx.send(f"nome ambiguo: {nomes}. escreva mais do nome.")
                    return None, None
    if ficha is None:
        if minhas:
            ficha = minhas[0]
        elif tokens:
            await ctx.send(f"nao encontrei nenhuma ficha chamada **{tokens[0][0]}** e voce nao tem ficha propria.")
            return None, None
        else:
            await ctx.send("voce ainda nao tem ficha. use `!criar [nome]`.")
            return None, None
    if sem_resto and resto:
        await ctx.send(f"nao entendi **{resto[0][0]}**. use o nome da ficha ou a ordem (`#1`, `#2`...).")
        return None, None
    if editar:
        if not await checar_edicao(ctx, ficha):
            return None, None
        registrar_alvo(ctx, ficha)
    return ficha, resto


def detectar_tipo_mensagem(texto: str) -> str:
    t = texto.lower()
    if any(p in t for p in ("nao", "erro", "invalido", "nao pode", "nao encontrei", "ambiguo", "so admins", "so o dono", "bloqueado", "cancelado")):
        return "erro"
    if any(p in t for p in ("uso:", "ex.:", "exemplo", "formato", "precisa", "separe")):
        return "info"
    if any(p in t for p in ("criada", "copiada", "atualizado", "restaurado", "apagada", "mudou", "agora", "feito", "sucesso", "vencedor", "vence")):
        return "sucesso"
    if any(p in t for p in ("aviso", "tempo esgotado", "aten\u00e7\u00e3o", "cancelado")):
        return "alerta"
    return "info"


def formatar_texto_discord(texto: str) -> str:
    """padroniza a apresentacao do texto para o Discord sem quebrar markdown ja usado pelo bot."""
    if not isinstance(texto, str):
        return texto
    texto = re.sub(r"\n{3,}", "\n\n", texto.strip())
    if not texto:
        return texto
    if any(token in texto for token in ("**", "*", "_", "`", "||", ">", "```")):
        return texto
    if texto.startswith(tuple("✅❌⚠️ℹ️⚔️🛡️🎲💰📜🔒🐛🧠✨📌⚙️💡♻️🗑️📄⭐🥊🔮🎯🔁🧾🕘💀🤝➡️")):
        return texto
    if "\n" in texto and (len(texto.splitlines()) > 2 or re.search(r"(?:^|\n)\s*(?:[-*•]|\d+\.)\s", texto)):
        return f"```md\n{texto}\n```"
    prefixos = {"erro": "❌", "alerta": "⚠️", "sucesso": "✅", "info": "ℹ️"}
    return f"{prefixos.get(detectar_tipo_mensagem(texto), 'ℹ️')} {texto}"


_send_original = commands.Context.send


async def send_formatado(self, *args, **kwargs):
    if args and isinstance(args[0], str):
        args = (formatar_texto_discord(args[0]),) + args[1:]
    elif "content" in kwargs and isinstance(kwargs["content"], str):
        kwargs["content"] = formatar_texto_discord(kwargs["content"])
    return await _send_original(self, *args, **kwargs)


commands.Context.send = send_formatado


# bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    case_insensitive=True,
    help_command=None,
    allowed_mentions=discord.AllowedMentions.none(),  # texto de jogadores nunca marca ninguem
)


@bot.check
async def so_em_servidor(ctx):
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return True


@tasks.loop(hours=6)
async def backup_periodico():
    fazer_backup()


@bot.event
async def on_ready():
    print(f"conectado como {bot.user} (id {bot.user.id})")
    if not backup_periodico.is_running():
        backup_periodico.start()
    if not limpar_dungeons.is_running():
        limpar_dungeons.start()


@bot.event
async def on_command_error(ctx, erro):
    if isinstance(erro, commands.CommandNotFound):
        return
    if isinstance(erro, commands.NoPrivateMessage):
        await ctx.send("use os comandos dentro de um servidor.")
        return
    if isinstance(erro, EmCombate):
        await ctx.send("⚔️ voce esta em combate numa dungeon: esse comando so volta a valer depois da luta. "
                       "na sua vez use `!atacar`, `!usar`, `!descansar`, `!fugir` ou `!passar`.")
        return
    if isinstance(erro, commands.CommandInvokeError):
        erro = erro.original
    traceback.print_exception(type(erro), erro, erro.__traceback__)
    await ctx.send("⚠️ ocorreu um erro inesperado. confira o formato do comando com `!ajuda`.")


class ConfirmarView(discord.ui.View):
    """botoes confirmar / cancelar; so quem usou o comando pode responder."""

    def __init__(self, autor_id):
        super().__init__(timeout=30)
        self.autor_id = autor_id
        self.resultado = None

    async def _responder(self, interaction, valor):
        if interaction.user.id != self.autor_id:
            await interaction.response.send_message("so quem usou o comando pode responder.", ephemeral=True)
            return
        self.resultado = valor
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="✅ confirmar", style=discord.ButtonStyle.success)
    async def btn_confirmar(self, interaction, button):
        await self._responder(interaction, True)

    @discord.ui.button(label="❌ cancelar", style=discord.ButtonStyle.secondary)
    async def btn_cancelar(self, interaction, button):
        await self._responder(interaction, False)


async def confirmar(ctx, texto: str) -> bool:
    view = ConfirmarView(ctx.author.id)
    msg = await ctx.send(f"⚠️ {texto}", view=view)
    await view.wait()
    if view.resultado is None:
        try:
            await msg.edit(content=f"⚠️ {texto}\n⌛ tempo esgotado, cancelado.", view=None)
        except discord.HTTPException as erro:
            print(f"nao foi possivel atualizar a confirmacao expirada: {erro}")
        return False
    if not view.resultado:
        await ctx.send("cancelado.")
        return False
    return True


# comandos que mudam a ficha e entram no historico (so se algo mudou de fato no banco)
COMANDOS_QUE_ALTERAM = {
    "mudar", "nivel", "pegar", "descartar", "equipar", "desequipar", "usaritem", "addstatus", "rmstatus",
    "renomearitem", "renomearstatus", "renomearnota", "anotar", "editarnota", "apagar", "limpar", "queimar",
    "ch", "usarh", "desativarh", "apagarh", "renomearh",
    "renomear", "mudardono", "dano", "turno", "upar",
    "transferir", "pix", "comprar", "vender", "darmoedas", "usar",
}


@bot.before_invoke
async def antes_do_comando(ctx):
    ctx._alteracoes_antes = db.total_changes


@bot.after_invoke
async def depois_do_comando(ctx):
    alvos = getattr(ctx, "_alvos", None)
    if not alvos or ctx.command is None or ctx.command.name not in COMANDOS_QUE_ALTERAM:
        return
    if db.total_changes == getattr(ctx, "_alteracoes_antes", None):
        return  # o comando nao mudou nada (ex.: erro de validacao)
    texto = ctx.message.content[:300]
    agora = int(time.time())
    for fid in alvos:
        if not get_ficha_id(fid):
            continue
        db.execute("INSERT INTO historico (ficha_id, user_id, texto, criado_em) VALUES (?, ?, ?, ?)",
                   (fid, ctx.author.id, texto, agora))
        db.execute("DELETE FROM historico WHERE ficha_id=? AND id NOT IN "
                   "(SELECT id FROM historico WHERE ficha_id=? ORDER BY id DESC LIMIT 100)", (fid, fid))
    db.commit()


AJUDA = [
    # (categoria, comando, uso, descricao, exemplo)
    ("fichas", "fichas", "!fichas [@pessoa] [pagina]", "lista as fichas do servidor (15 por pagina), com a ordem de cada uma", "!fichas @maria 2"),
    ("fichas", "criar", "!criar [nome] [@dono]", "cria uma ficha vazia; o @dono e so para admins; o limite de fichas por pessoa e definido pelos admins", "!criar Kage"),
    ("fichas", "deletar", "!deletar [nome1] [nome2] ...", "apaga uma ou varias fichas (pede confirmacao; da para desfazer com !restaurar); exige o nome ou pelo menos 3 letras do comeco", "!deletar Kage Douma"),
    ("fichas", "olhar", "!olhar [nome]", "mostra a ficha completa; sem nome mostra as suas", "!olhar kage"),
    ("fichas", "renomear", "!renomear [pers] [novo nome]", "troca o nome da ficha", "!renomear Kage KageYaeger"),
    ("fichas", "ordem", "!ordem [pers] [posicao]", "sem argumentos mostra a ordem das suas fichas; com personagem e posicao move a ficha; a #1 e a padrao dos comandos", "!ordem #2 1"),
    ("fichas", "copiar", "!copiar [pers] [novo nome] [@dono]", "copia a ficha inteira (atributos, itens, status, habilidades e notas)", "!copiar Kage Clone"),
    ("fichas", "deletartudo", "!deletartudo [@pessoa]", "apaga todas as suas fichas (admins podem indicar outra pessoa); pede confirmacao", ""),
    ("atributos", "mudar", "!mudar [pers] [sigla] [valor|+n|-n] [sigla2] [valor2] ...", "muda atributos (for res agi com int sab car pre) e corpo/mente (crp mnt); vigor e vontade sao calculados; crp/mnt aceitam % do maximo (crp -50% tira metade do vigor; crp 50% define em 50%)", "!mudar for +2 res -1 agi 3"),
    ("atributos", "nivel", "!nivel [pers] [valor|+n|-n]", "ve ou muda o nivel (1 a 20); -1 ou livre = sem limite de pontos", "!nivel 5"),
    ("atributos", "vida", "!vida [pers]", "mostra corpo, mente e as penalidades atuais", ""),
    ("combate", "d20", "!d20 | !d20 [pers] [sigla] [habilidade] | !d20 [pers] [habilidade] [sigla] | !d20 [pers1] [pers2] [sigla]", "sem nada rola um d20 puro; com sigla rola d20 + atributo; com a habilidade depois da sigla soma o bonus dela na rolagem; com a habilidade antes da sigla rola so o valor da habilidade (50% de int = metade do int da ficha); com dois ou mais personagens e a sigla no fim e uma briga: o maior vence (diferenca de ate 4 e empate)", "!d20 gelo int"),
    ("combate", "iniciativa", "!iniciativa [pers1] [pers2] ...", "cada um rola agilidade e a ordem sai do maior para o menor, com desempate automatico", "!iniciativa Kage Douma"),
    ("combate", "dano", "!dano [rolagem de dano] [res|sab] [rolagem de defesa] [pers]", "calcula o dano (diferenca de ate 4 = sem dano); res tira do corpo, sab da mente; com o personagem no fim, aplica o dano", "!dano 20 res 12 kage"),
    ("combate", "turno", "!turno [pers1, pers2, ...]", "passa o turno: aplica status dinamicos e reduz a duracao de status e habilidades", "!turno Kage, Douma"),
    ("itens", "inventario", "!inventario [pers]", "mostra o inventario", ""),
    ("itens", "pegar", "!pegar [pers] [item] [qtd] [tipo] [+sigla,-sigla] [valores]", "adiciona item; tipo normal (equipavel, da bonus) ou consumível (efeito em crp/mnt ao usar); valores aceitam %", "!pegar espada 1 normal +for,-agi 3,1"),
    ("itens", "descartar", "!descartar [pers] [item] [qtd]", "remove o item (ou so a quantidade informada); exige o nome ou pelo menos 3 letras do comeco", "!descartar pocao 2"),
    ("itens", "equipar", "!equipar [pers] [item]", "equipa uma unidade do item (o bonus soma por unidade)", "!equipar espada"),
    ("itens", "desequipar", "!desequipar [pers] [item]", "tira uma unidade equipada", "!desequipar espada"),
    ("itens", "equipados", "!equipados [pers]", "mostra os itens equipados e o bonus total", ""),
    ("itens", "usaritem", "!usaritem [pers] [item]", "usa um consumível: gasta uma unidade e aplica o efeito em corpo/mente", "!usaritem pocao"),
    ("itens", "renomearitem", "!renomearitem [pers] [item] [novo nome]", "troca o nome do item", ""),
    ("itens", "transferir", "!transferir [pers] [item] [qtd|tudo] [destino]", "passa itens de uma ficha sua para outra ficha (de qualquer jogador); dentro da dungeon so entre o mesmo grupo e fora do combate", "!transferir \"Pocao de Cura\" 2 Douma"),
    ("economia", "saldo", "!saldo [pers]", "mostra as moedas da ficha; elas vem das dungeons, dos eventos e das vendas", ""),
    ("economia", "pix", "!pix [pers] [moedas] [destino]", "transfere moedas para outra ficha; dentro da dungeon, so entre o mesmo grupo e fora do combate", "!pix Kage 50 Douma"),
    ("economia", "loja", "!loja", "mostra os itens e habilidades a venda e seus precos (equipamentos melhores pedem nivel minimo)", ""),
    ("economia", "comprar", "!comprar [pers] [item|habilidade] [qtd]", "compra um item (com quantidade) ou aprende uma habilidade da loja; nao funciona dentro de dungeon", "!comprar pocao de cura 3"),
    ("economia", "vender", "!vender [pers] [item] [qtd|tudo]", "vende pela metade do preco; so itens comprados ou achados nas dungeons/eventos (nao os criados com !pegar)", "!vender espada de ferro"),
    ("economia", "darmoedas", "!darmoedas [pers] [quantidade]", "admin: da (ou tira, com -) moedas de uma ficha", "!darmoedas kage 200"),
    ("itens", "limpar", "!limpar [pers]", "apaga todos os itens do inventario (pede confirmacao; da para desfazer)", ""),
    ("status", "addstatus", "!addstatus [pers] [nome] [dinamico|estatico] [+sigla,-sigla] [valores] [turnos]", "dinamico muda corpo/mente a cada turno; estatico mantem bonus ou penalidade; turnos e opcional; valores aceitam % (-crp 10% = 10% do vigor por turno)", "!addstatus veneno dinamico -crp 3 4"),
    ("status", "status", "!status [pers]", "mostra os status ativos", ""),
    ("status", "rmstatus", "!rmstatus [pers] [nome]", "remove um status; exige o nome ou pelo menos 3 letras do comeco", ""),
    ("status", "renomearstatus", "!renomearstatus [pers] [nome] [novo nome]", "troca o nome do status", ""),
    ("habilidades", "ch", "!ch [pers] [nome] [+sigla,-sigla] [valores] [persistente|turnos]", "cria habilidade; sem duracao vale so o turno atual; crp/mnt tem efeito imediato ao usar; valores aceitam % (+crp 50% = metade do vigor; +for 20% = 20% do atributo); int causa dano fisico", "!ch Kage CorteLinear +for,+agi 2,5"),
    ("habilidades", "habilidades", "!habilidades [pers]", "lista as habilidades e quais estao ativas; habilidades da loja sao aprendidas com !comprar", ""),
    ("habilidades", "usarh", "!usarh [pers] [habilidade] [alvo opcional]", "ativa a habilidade; na dungeon, melhoria (mel, incluindo cura) pode mirar aliado e penalidade (pen) inimigo; cura/vital e imediata, bonus de atributo dura o tempo definido", "!usarh Cura Douma"),
    ("habilidades", "desativarh", "!desativarh [pers] [habilidade]", "desativa uma habilidade ativa", ""),
    ("habilidades", "apagarh", "!apagarh [pers] [habilidade]", "apaga a habilidade; exige o nome ou pelo menos 3 letras do comeco", ""),
    ("habilidades", "renomearh", "!renomearh [pers] [habilidade] [novo nome]", "troca o nome da habilidade", ""),
    ("notas", "anotar", "!anotar [pers] [nome da nota] [texto]", "cria uma nota (poderes, pericias, o que quiser)", "!anotar Kage poder Bankai ..."),
    ("notas", "notas", "!notas [pers] [nota]", "mostra todas as notas ou uma so", ""),
    ("notas", "editarnota", "!editarnota [pers] [nota] [novo texto]", "troca o texto da nota", ""),
    ("notas", "renomearnota", "!renomearnota [pers] [nota] [novo nome]", "troca o nome da nota", ""),
    ("notas", "apagar", "!apagar [pers] [nota]", "apaga uma nota; exige o nome ou pelo menos 3 letras do comeco", ""),
    ("notas", "queimar", "!queimar [pers]", "apaga todas as notas (pede confirmacao; da para desfazer)", ""),
    ("xp", "xp", "!xp [pers]", "mostra o nivel e o xp da ficha; xp vem das dungeons e cada nivel pede 100 x nivel de xp (ficha em nivel livre nao usa xp)", "!xp kage"),
    ("xp", "upar", "!upar [pers] [quantidade]", "admin: da (ou tira, com -) xp a uma ficha; sobe de nivel sozinho", "!upar kage 150"),
    ("dungeon", "dungeon", "!dungeon", "mostra as dificuldades, os temas e os comandos; **corpo em 0 = a ficha e apagada para sempre**", ""),
    ("dungeon", "dungeon criar", "!dungeon criar [facil|medio|dificil|pesadelo] [caverna|cripta|floresta|ruinas] [ficha]", "abre uma dungeon no canal; niveis sugeridos: facil 1-5, medio 6-10, dificil 11-15, pesadelo 16-20. qualquer ficha pode entrar em qualquer uma; inimigos acompanham o nivel medio do grupo; pede confirmacao por causa da morte permanente", "!dungeon criar dificil cripta kage"),
    ("dungeon", "dungeon entrar", "!dungeon entrar [ficha]", "entra no grupo antes do inicio (ate 4 jogadores, uma ficha por pessoa; qualquer nivel pode entrar em qualquer dificuldade)", "!dungeon entrar douma"),
    ("dungeon", "dungeon iniciar", "!dungeon iniciar", "o lider comeca a dungeon (pode ser solo)", ""),
    ("dungeon", "dungeon avancar", "!dungeon avancar", "no acampamento entre andares, o lider desce ao proximo andar", ""),
    ("dungeon", "dungeon status", "!dungeon status", "mostra o andar, os inimigos, o grupo e de quem e a vez", ""),
    ("dungeon", "dungeon sair", "!dungeon sair", "sai do grupo no lobby ou no acampamento, mantendo o que ja ganhou", ""),
    ("dungeon", "dungeon ranking", "!dungeon ranking", "ranking da dungeon atual: abates, dano e acertos de cada um; no fim o mvp ganha moedas extras", ""),
    ("dungeon", "escolher", "!escolher [numero ou opcao]", "em eventos de grupo, todos votam e o efeito atinge o grupo; em eventos individuais, apenas o explorador indicado escolhe e recebe o efeito", "!escolher examinar"),
    ("dungeon", "ranking", "!ranking [abates|mvp|dano|vitorias]", "ranking do servidor somando todas as dungeons", "!ranking mvp"),
    ("dungeon", "atacar", "!atacar [alvo] [perto|longe|magia|jujutsu] [habilidade opcional]", "perto: combate+for; longe: precisao+for+metade da int; magia: precisao+int; jujutsu: combate+for+metade da int; metade da int arredonda para baixo e todo dano tira corpo", "!atacar 2 jujutsu Pesadelo"),
    ("dungeon", "usar", "!usar [consumível] [alvo aliado opcional]", "gasta a vez usando um consumível da sua ficha; pode aplicar em qualquer aliado do grupo", "!usar \"Pocao de Cura\" Douma"),
    ("dungeon", "descansar", "!descansar [crp|mnt]", "em combate gasta a vez e recupera vigor/20 (ou vontade/20); no acampamento recupera 20%, uma vez por pessoa", "!descansar mnt"),
    ("dungeon", "fugir", "!fugir", "foge so da batalha (a dungeon continua): rola agilidade contra o inimigo mais agil e so falha se perder por mais de 4; "
     "quem foge fica sem a recompensa do andar e volta no acampamento; se todos fugirem o andar e refeito", ""),
    ("dungeon", "passar", "!passar", "passa a sua vez sem fazer nada", ""),
    ("seguranca", "historico", "!historico [pers] [quantidade]", "mostra as ultimas alteracoes da ficha (quem fez e quando); so o dono e admins veem", "!historico kage 15"),
    ("seguranca", "restaurar", "!restaurar [lista|numero]", "desfaz a ultima exclusao (fichas, itens ou notas) feita nos ultimos 30 minutos", "!restaurar lista"),
    ("admin", "mudardono", "!mudardono [pers] [@novo dono]", "passa a ficha para outra pessoa", ""),
    ("admin", "limitefichas", "!limitefichas [@pessoa] [n|padrao]", "define quantas fichas essa pessoa pode ter; sem n mostra o limite", ""),
    ("admin", "limitepadrao", "!limitepadrao [n]", "define o limite padrao de fichas por pessoa", ""),
]
for i, entrada in enumerate(AJUDA):
    if entrada[1] == "ch":
        AJUDA[i] = (
            entrada[0], entrada[1],
            "!ch [pers] [nome] [+sigla,-sigla] [valores] [persistente|turnos] [perto|longe|magia|jujutsu|melhoria|mel|penalidade|pen]",
            "cria habilidade de ataque, melhoria (mel) ou penalidade (pen); crp/mnt tem efeito imediato",
            "!ch Kage CorteLinear +for,+agi 2,5 perto",
        )
    elif entrada[1] == "usaritem":
        AJUDA[i] = (
            entrada[0], entrada[1], "!usaritem [pers] [item] [alvo opcional do grupo]",
            "usa um consumível; na dungeon pode aplicar em qualquer aliado do grupo", "!usaritem pocao Douma",
        )
CATEGORIAS = ["fichas", "atributos", "xp", "combate", "dungeon", "itens", "economia", "status", "habilidades",
              "notas", "seguranca", "admin"]


@bot.command(name="ajuda")
async def ajuda(ctx, *, raw: str = ""):
    arg = norm(raw).lstrip("!").strip()
    if not arg:
        linhas = [f"**{c.title()}**\n" + " • ".join(f"`!{e[1]}`" for e in AJUDA if e[0] == c)
                  for c in CATEGORIAS]
        await enviar_longo(
            ctx,
            "📚 **Central de ajuda**\n"
            "Consulte `!ajuda [categoria]` para ver os comandos de uma área ou `!ajuda [comando]` "
            "para consultar um comando específico.\n\n"
            + "\n\n".join(linhas)
            + "\n\n**Dicas rápidas**\n"
              "• Use aspas em nomes com espaços.\n"
              "• Você pode indicar a ficha pela ordem (`#2`) ou pelo começo do nome.\n"
              "• Se não informar a ficha, será usada a sua ficha #1.\n"
              "• Alguns valores aceitam porcentagem: `+crp 50%` recupera metade do máximo; "
              "`+for 20%` vale 20% do atributo."
        )
        return
    if arg in CATEGORIAS:
        entradas = [e for e in AJUDA if e[0] == arg]
        blocos = [f"`{e[2]}`\n{e[3]}" + (f"\nExemplo: `{e[4]}`" if e[4] else "")
                  for e in entradas]
        await enviar_longo(ctx, f"📚 **Ajuda · {arg.title()}**\n\n" + "\n\n".join(blocos))
        return
    achados = [e for e in AJUDA if e[1] == arg]
    if not achados:
        achados = [e for e in AJUDA if e[1].startswith(arg)]
        cats = [c for c in CATEGORIAS if c.startswith(arg)]
        if len(achados) + len(cats) != 1:
            if not achados and not cats:
                await ctx.send(f"nao conheco **{arg}**. use `!ajuda` para ver as categorias.")
            else:
                opcoes = [f"`{e[1]}`" for e in achados] + [f"`{c}`" for c in cats]
                await ctx.send("qual deles? " + ", ".join(opcoes))
            return
        if cats:
            await ajuda(ctx, raw=cats[0])
            return
    e = achados[0]
    texto = f"📚 **Comando: !{e[1]}**\n\n**O que faz:** {e[3]}\n\n**Como usar:** `{e[2]}`"
    if e[4]:
        texto += f"\n\n**Exemplo:** `{e[4]}`"
    await enviar_longo(ctx, texto)


# fichas
@bot.command(name="fichas")
async def fichas(ctx, *, raw: str = ""):
    dono = ctx.message.mentions[0] if ctx.message.mentions else None
    resto = re.sub(r"<@!?\d+>", "", raw).strip()
    if resto and not (resto.isdigit() and int(resto) >= 1):
        await ctx.send("uso: `!fichas [@pessoa] [pagina]`")
        return
    pagina = int(resto) if resto else 1
    if dono:
        rows = db.execute("SELECT nome, dono_id, nivel, ordem FROM fichas WHERE guild_id=? AND dono_id=? ORDER BY ordem",
                          (ctx.guild.id, dono.id)).fetchall()
    else:
        rows = db.execute("SELECT nome, dono_id, nivel, ordem FROM fichas WHERE guild_id=? ORDER BY nome",
                          (ctx.guild.id,)).fetchall()
    if not rows:
        await ctx.send("nenhuma ficha encontrada. use `!criar [nome]`.")
        return
    por_pagina = 15
    paginas = (len(rows) + por_pagina - 1) // por_pagina
    if pagina > paginas:
        await ctx.send(f"so existem {paginas} pagina(s).")
        return
    fatia = rows[(pagina - 1) * por_pagina: pagina * por_pagina]
    linhas = [f"• **{r['nome']}** · nível {fmt_nivel(r['nivel'])} · ficha #{r['ordem']} · <@{r['dono_id']}>"
              for r in fatia]
    titulo = f"📚 **Fichas encontradas: {len(rows)}**" + (f" · jogador <@{dono.id}>" if dono else "")
    if paginas > 1:
        titulo += f" · página {pagina}/{paginas}"
    texto = titulo + "\n" + "\n".join(linhas)
    if pagina < paginas:
        texto += f"\n\nPróxima página: `!fichas [@pessoa] {pagina + 1}`"
    await enviar_longo(ctx, texto)


@bot.command(name="criar", aliases=["criarficha", "criarfichas"])
async def criarficha(ctx, *, raw: str = ""):
    dono = ctx.author
    if ctx.message.mentions:
        dono = ctx.message.mentions[0]
        if dono.bot:
            await ctx.send("nao da para criar ficha para um bot.")
            return
        if dono.id != ctx.author.id and not eh_admin(ctx):
            await ctx.send("🚫 so admins podem criar ficha para outra pessoa.")
            return
    nome = re.sub(r"<@!?\d+>", "", raw).strip().strip(ASPAS).strip()
    if not nome:
        await ctx.send("uso: `!criar [nome] [@dono]` (o @dono e so para admins)")
        return
    if len(nome) > 32 or "," in nome:
        await ctx.send("o nome deve ter ate 32 caracteres e nao pode conter virgula.")
        return
    if get_ficha(ctx.guild.id, nome, exato=True):
        await ctx.send(f"ja existe uma ficha chamada **{nome}**. os nomes precisam ser unicos.")
        return
    if not eh_admin(ctx):
        limite = limite_de(ctx.guild.id, dono.id)
        if len(fichas_do_usuario(ctx.guild.id, dono.id)) >= limite:
            await ctx.send(f"voce ja tem {limite} ficha(s), o maximo permitido para voce.")
            return
    db.execute("INSERT INTO fichas (guild_id, dono_id, nome, ordem) VALUES (?, ?, ?, 999999)",
               (ctx.guild.id, dono.id, nome))
    db.commit()
    renumerar(ctx.guild.id, dono.id)
    if dono.id == ctx.author.id:
        await ctx.send(f"✅ ficha **{nome}** criada. distribua seus pontos com `!mudar \"{nome}\" for 3`, por exemplo.")
    else:
        await ctx.send(f"✅ ficha **{nome}** criada para <@{dono.id}>.")


@bot.command(name="deletar", aliases=["deletarficha", "deletarfichas"])
async def deletarficha(ctx, *, raw: str = ""):
    bruto = raw.strip()
    minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
    if not bruto:
        nomes = []
    elif get_ficha(ctx.guild.id, bruto.strip(ASPAS).strip(), exato=True):
        nomes = [bruto]   # um nome so, mesmo com espacos
    elif "," in bruto:
        nomes = [n for n in bruto.split(",") if n.strip()]
    else:
        nomes = [t for t, _ in tokenizar(bruto)]
    alvos, avisos, vistos = [], [], set()
    if not nomes:
        if not minhas:
            await ctx.send("voce ainda nao tem ficha.")
            return
        alvos = [minhas[0]]
    for n in nomes:
        n = n.strip().strip(ASPAS).strip()
        f = get_ficha(ctx.guild.id, n, seguro=True)
        if not f:
            m = re.fullmatch(r"#?(\d+)", n)
            if m and 1 <= int(m.group(1)) <= len(minhas):
                f = minhas[int(m.group(1)) - 1]
        if not f:
            avisos.append(msg_ficha_nao_achada(ctx.guild.id, n))
            continue
        if not pode_editar(ctx, f):
            avisos.append(f"🚫 voce nao pode apagar **{f['nome']}**.")
            continue
        if run_da_ficha(f["id"]):
            avisos.append(f"⚔️ **{f['nome']}** esta em uma dungeon e nao pode ser apagada agora.")
            continue
        if f["id"] in vistos:
            continue
        vistos.add(f["id"])
        alvos.append(f)
    if avisos:
        await ctx.send("\n".join(avisos))
    if not alvos:
        return
    if len(alvos) == 1:
        pergunta = f"apagar **{alvos[0]['nome']}** com itens, status, habilidades e notas?"
    else:
        lista = ", ".join(f"**{f['nome']}**" for f in alvos)
        pergunta = f"apagar {len(alvos)} fichas ({lista}) com itens, status, habilidades e notas?"
    if not await confirmar(ctx, pergunta):
        return
    guardar_lixeira(ctx, "fichas", ", ".join(f["nome"] for f in alvos), [serializar_ficha(f) for f in alvos])
    for f in alvos:
        db.execute("DELETE FROM fichas WHERE id=?", (f["id"],))
    db.commit()
    for dono in {f["dono_id"] for f in alvos}:
        renumerar(ctx.guild.id, dono)
    if len(alvos) == 1:
        await ctx.send(f"🗑️ ficha **{alvos[0]['nome']}** apagada.")
    else:
        await ctx.send(f"🗑️ {len(alvos)} fichas apagadas.")


@bot.command(name="olhar", aliases=["olharficha"])
async def olharficha(ctx, *, raw: str = ""):
    nome = raw.strip().strip(ASPAS).strip()
    minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
    if nome:
        f = get_ficha(ctx.guild.id, nome)
        if not f:
            m = re.fullmatch(r"#?(\d+)", nome)
            if m and 1 <= int(m.group(1)) <= len(minhas):
                f = minhas[int(m.group(1)) - 1]
        if not f:
            await ctx.send(msg_ficha_nao_achada(ctx.guild.id, nome))
            return
        await ctx.send(embed=embed_ficha(f))
        return
    if not minhas:
        await ctx.send("voce ainda nao tem ficha. use `!criar [nome]`.")
        return
    for f in minhas:
        await ctx.send(embed=embed_ficha(f))


@bot.command(name="renomear", aliases=["renomearficha"])
async def renomearficha(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades=lambda n: n >= 1)
    if not f:
        return
    if not resto:
        await ctx.send("uso: `!renomear [pers] [novo nome]`")
        return
    novo = raw[resto[0][1]:].strip().strip(ASPAS).strip()
    if len(novo) > 32 or "," in novo or not novo:
        await ctx.send("o nome deve ter de 1 a 32 caracteres e nao pode conter virgula.")
        return
    outra = get_ficha(ctx.guild.id, novo, exato=True)
    if outra and outra["id"] != f["id"]:
        await ctx.send(f"ja existe uma ficha chamada **{novo}**.")
        return
    db.execute("UPDATE fichas SET nome=? WHERE id=?", (novo, f["id"]))
    db.commit()
    await ctx.send(f"✏️ **{f['nome']}** agora se chama **{novo}**.")


def nome_de_copia(guild_id, original: str) -> str:
    sufixo = " (copia)"
    nome = original[:32 - len(sufixo)] + sufixo
    n = 2
    while get_ficha(guild_id, nome, exato=True):
        suf = f" (copia {n})"
        nome = original[:32 - len(suf)] + suf
        n += 1
    return nome


@bot.command(name="copiar", aliases=["copiarficha"])
async def copiarficha(ctx, *, raw: str = ""):
    """copia uma ficha inteira (atributos, nivel, corpo/mente, itens, status, habilidades e notas)."""
    dono = ctx.author
    if ctx.message.mentions:
        dono = ctx.message.mentions[0]
        if dono.bot:
            await ctx.send("nao da para dar ficha a um bot.")
            return
        if dono.id != ctx.author.id and not eh_admin(ctx):
            await ctx.send("🚫 so admins podem criar a copia para outra pessoa.")
            return
    limpo = re.sub(r"<@!?\d+>", "", raw)
    f, resto = await resolver(ctx, tokenizar(limpo), aridades=lambda n: n >= 0)
    if not f:
        return
    if resto:
        nome = limpo[resto[0][1]:].strip().strip(ASPAS).strip()
    else:
        nome = nome_de_copia(ctx.guild.id, f["nome"])
    if not nome or len(nome) > 32 or "," in nome:
        await ctx.send("o nome da copia deve ter de 1 a 32 caracteres e nao pode conter virgula.")
        return
    if get_ficha(ctx.guild.id, nome, exato=True):
        await ctx.send(f"ja existe uma ficha chamada **{nome}**. os nomes precisam ser unicos.")
        return
    if not eh_admin(ctx):
        limite = limite_de(ctx.guild.id, dono.id)
        if len(fichas_do_usuario(ctx.guild.id, dono.id)) >= limite:
            await ctx.send(f"voce ja tem {limite} ficha(s), o maximo permitido para voce.")
            return
    cols = ["nivel", "exp"] + list(ATRIBUTOS) + ["corpo", "mente"]
    cur = db.execute(
        f"INSERT INTO fichas (guild_id, dono_id, nome, ordem, {', '.join(cols)}) "
        f"VALUES (?, ?, ?, 999999, {', '.join('?' for _ in cols)})",
        (ctx.guild.id, dono.id, nome, *[f[c] for c in cols]))
    novo_id = cur.lastrowid
    db.execute("INSERT INTO itens (ficha_id, nome, quantidade, bonus, equipado, tipo, vendaveis) "
               "SELECT ?, nome, quantidade, bonus, equipado, tipo, vendaveis FROM itens WHERE ficha_id=?",
               (novo_id, f["id"]))
    db.execute("INSERT INTO status (ficha_id, nome, tipo, bonus, duracao) "
               "SELECT ?, nome, tipo, bonus, duracao FROM status WHERE ficha_id=?", (novo_id, f["id"]))
    db.execute("INSERT INTO notas (ficha_id, titulo, texto) "
               "SELECT ?, titulo, texto FROM notas WHERE ficha_id=?", (novo_id, f["id"]))
    db.execute("INSERT INTO habilidades (ficha_id, nome, bonus, duracao, ativa, restam, modo, mental, requisitos) "
               "SELECT ?, nome, bonus, duracao, ativa, restam, modo, mental, requisitos FROM habilidades WHERE ficha_id=?",
               (novo_id, f["id"]))
    db.commit()
    renumerar(ctx.guild.id, dono.id)
    normalizar_vitais(novo_id)
    qi = db.execute("SELECT COUNT(*) c FROM itens WHERE ficha_id=?", (novo_id,)).fetchone()["c"]
    qs = db.execute("SELECT COUNT(*) c FROM status WHERE ficha_id=?", (novo_id,)).fetchone()["c"]
    qn = db.execute("SELECT COUNT(*) c FROM notas WHERE ficha_id=?", (novo_id,)).fetchone()["c"]
    qh = db.execute("SELECT COUNT(*) c FROM habilidades WHERE ficha_id=?", (novo_id,)).fetchone()["c"]
    de_quem = "" if dono.id == ctx.author.id else f" para <@{dono.id}>"
    await ctx.send(f"📄 **{f['nome']}** copiada como **{nome}**{de_quem} "
                   f"({qi} item(ns), {qs} status, {qn} nota(s), {qh} habilidade(s)).")


@bot.command(name="mudardono")
async def mudardono(ctx, *, raw: str = ""):
    if not eh_admin(ctx):
        await ctx.send("🚫 so admins podem mudar o dono de uma ficha.")
        return
    if not ctx.message.mentions or ctx.message.mentions[0].bot:
        await ctx.send("uso: `!mudardono [personagem] [@novo dono]`")
        return
    novo = ctx.message.mentions[0]
    nome = re.sub(r"<@!?\d+>", "", raw).strip().strip(ASPAS).strip()
    f = get_ficha(ctx.guild.id, nome) if nome else None
    if not f:
        await ctx.send(msg_ficha_nao_achada(ctx.guild.id, nome) if nome else "uso: `!mudardono [personagem] [@novo dono]`")
        return
    if run_da_ficha(f["id"]):
        await ctx.send(f"⚔️ **{f['nome']}** esta em uma dungeon; espere ela terminar.")
        return
    dono_antigo = f["dono_id"]
    db.execute("UPDATE fichas SET dono_id=?, ordem=999999 WHERE id=?", (novo.id, f["id"]))
    db.commit()
    renumerar(ctx.guild.id, dono_antigo)
    renumerar(ctx.guild.id, novo.id)
    msg = f"👤 **{f['nome']}** agora pertence a <@{novo.id}>."
    qtd = len(fichas_do_usuario(ctx.guild.id, novo.id))
    limite = limite_de(ctx.guild.id, novo.id)
    if qtd > limite:
        msg += f"\n⚠️ essa pessoa agora tem {qtd} fichas, acima do limite dela ({limite})."
    await ctx.send(msg)


@bot.command(name="limitefichas")
async def limitefichas(ctx, *, raw: str = ""):
    if not eh_admin(ctx):
        await ctx.send("🚫 so admins podem mudar limites de fichas.")
        return
    if not ctx.message.mentions:
        await ctx.send("uso: `!limitefichas [@pessoa] [n]` (sem n mostra o limite; `padrao` volta ao limite padrao)")
        return
    alvo = ctx.message.mentions[0]
    resto = re.sub(r"<@!?\d+>", "", raw).strip()
    if not resto:
        await ctx.send(f"limite de <@{alvo.id}>: **{limite_de(ctx.guild.id, alvo.id)}** ficha(s) "
                       f"(padrao do servidor: {limite_padrao(ctx.guild.id)}).")
        return
    if norm(resto) in ("padrao", "reset"):
        db.execute("DELETE FROM limites WHERE guild_id=? AND user_id=?", (ctx.guild.id, alvo.id))
        db.commit()
        await ctx.send(f"↩️ <@{alvo.id}> voltou ao limite padrao ({limite_padrao(ctx.guild.id)}).")
        return
    if not resto.isdigit() or not 0 <= int(resto) <= 50:
        await ctx.send("o limite precisa ser um numero de 0 a 50.")
        return
    db.execute("INSERT INTO limites (guild_id, user_id, limite) VALUES (?, ?, ?) "
               "ON CONFLICT(guild_id, user_id) DO UPDATE SET limite=excluded.limite",
               (ctx.guild.id, alvo.id, int(resto)))
    db.commit()
    await ctx.send(f"✅ <@{alvo.id}> agora pode ter ate **{int(resto)}** ficha(s).")


@bot.command(name="limitepadrao")
async def limitepadrao(ctx, *, raw: str = ""):
    if not eh_admin(ctx):
        await ctx.send("🚫 so admins podem mudar o limite padrao.")
        return
    raw = raw.strip()
    if not raw:
        await ctx.send(f"limite padrao atual: **{limite_padrao(ctx.guild.id)}** ficha(s) por pessoa.")
        return
    if not raw.isdigit() or not 0 <= int(raw) <= 50:
        await ctx.send("uso: `!limitepadrao [n]` com n de 0 a 50.")
        return
    db.execute("INSERT INTO config (guild_id, limite_padrao) VALUES (?, ?) "
               "ON CONFLICT(guild_id) DO UPDATE SET limite_padrao=excluded.limite_padrao",
               (ctx.guild.id, int(raw)))
    db.commit()
    await ctx.send(f"✅ limite padrao agora e **{int(raw)}** ficha(s) por pessoa.")


# atributos e nivel
@bot.command(name="mudar")
async def mudar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades=lambda n: n >= 2 and n % 2 == 0)
    if not f:
        return
    uso = ("uso: `!mudar [personagem] [sigla] [valor ou +n/-n]`\n"
           "varios de uma vez: `!mudar for +2 res -1 agi 3` ou `!mudar for,res,agi +2,-1,3`\n"
           "siglas: for res agi com int sab car pre crp mnt")
    textos = [t for t, _ in resto]
    if len(textos) == 2 and "," in textos[0]:
        campos = [c for c in textos[0].split(",") if c.strip()]
        valores = [v for v in textos[1].split(",") if v.strip()]
        if len(campos) != len(valores):
            await ctx.send("a quantidade de atributos e de valores precisa ser a mesma.\n" + uso)
            return
        pares = list(zip(campos, valores))
    elif textos and len(textos) % 2 == 0:
        pares = list(zip(textos[0::2], textos[1::2]))
    else:
        await ctx.send(uso)
        return

    # 1) valida tudo antes de alterar qualquer coisa
    digitados, mud_attr, mud_vit, erros = [], {}, {}, []
    for campo_txt, val_txt in pares:
        c = norm(campo_txt)
        if c in ("vigor", "vgr", "vontade", "vtd"):
            erros.append(f"**{campo_txt}**: vigor e vontade sao calculados (60 + res / 60 + sab) e nao podem ser mudados.")
            continue
        if c == "nivel":
            erros.append(f"**{campo_txt}**: para o nivel use `!nivel`.")
            continue
        campo = achar_attr(c) or VITAIS.get(c)
        pct = re.fullmatch(r"([+-])?(\d+)%", val_txt.strip())
        if campo and pct and campo not in ("corpo", "mente"):
            erros.append(f"**{campo_txt} {val_txt}**: porcentagem so vale para crp e mnt no !mudar.")
            continue
        pv = ("%", pct.group(1), int(pct.group(2))) if pct else parse_valor(val_txt)
        if not campo or pv is None:
            erros.append(f"**{campo_txt} {val_txt}**: atributo ou valor invalido.")
            continue
        if campo in digitados:
            erros.append(f"**{SIG[campo]}** apareceu mais de uma vez.")
            continue
        if campo in ATRIBUTOS:
            alvo = aplicar_valor(f[campo], pv)
            if not 0 <= alvo <= LIMITE_ATRIBUTO:
                erros.append(f"**{SIG[campo]}**: cada atributo vai de 0 a {LIMITE_ATRIBUTO} (o resultado seria {alvo}).")
                continue
            mud_attr[campo] = alvo
        else:
            mud_vit[campo] = pv
        digitados.append(campo)
    if erros:
        await ctx.send("nada foi alterado:\n" + "\n".join(f"• {e}" for e in erros) + "\n" + uso)
        return

    # 2) aplica: primeiro atributos, depois corpo/mente (ja com o novo vigor/vontade)
    bonus = get_bonus(f["id"])
    vigor_antigo, vontade_antiga = maximos(f, bonus)
    for campo, alvo in mud_attr.items():
        db.execute(f"UPDATE fichas SET {campo}=? WHERE id=?", (alvo, f["id"]))
    f2 = get_ficha_id(f["id"])
    vigor_novo, vontade_nova = maximos(f2, bonus)
    corpo, mente = f["corpo"], f["mente"]
    # se estava com corpo/mente cheios, acompanha o novo maximo
    if "resistencia" in mud_attr and corpo >= vigor_antigo:
        corpo = vigor_novo
    if "sabedoria" in mud_attr and mente >= vontade_antiga:
        mente = vontade_nova
    if "corpo" in mud_vit:
        corpo = aplicar_vital(corpo, mud_vit["corpo"], vigor_novo)
    if "mente" in mud_vit:
        mente = aplicar_vital(mente, mud_vit["mente"], vontade_nova)
    corpo = max(0, min(corpo, vigor_novo))
    mente = max(0, min(mente, vontade_nova))
    db.execute("UPDATE fichas SET corpo=?, mente=? WHERE id=?", (corpo, mente, f["id"]))
    db.commit()
    normalizar_vitais(f["id"])

    linhas = []
    for campo in digitados:
        if campo in ATRIBUTOS:
            linhas.append(f"{SIG[campo]}: {f[campo]} → {mud_attr[campo]}")
    if "corpo" in mud_vit or corpo != f["corpo"]:
        linhas.append(f"crp: {f['corpo']} → {corpo}")
    if "mente" in mud_vit or mente != f["mente"]:
        linhas.append(f"mnt: {f['mente']} → {mente}")
    if len(linhas) == 1:
        msg = f"✅ **{f['nome']}** — {linhas[0]}"
    else:
        msg = f"✅ **{f['nome']}**\n" + "\n".join(linhas)
    f3 = get_ficha_id(f["id"])
    usados = sum(f3[k] for k in ATRIBUTOS)
    if f3["nivel"] != -1 and usados > 4 * f3["nivel"]:
        msg += f"\n⚠️ pontos distribuidos ({usados}) acima do permitido para o nivel {f3['nivel']} ({4 * f3['nivel']})."
    await ctx.send(msg)


@bot.command(name="nivel")
async def nivel(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), numero_e_valor=True, aridades={0, 1})
    if not f:
        return
    if not resto:
        if f["nivel"] == -1:
            await ctx.send(f"**{f['nome']}** esta em nivel livre (sem limite de pontos de atributo).")
        else:
            await ctx.send(f"**{f['nome']}** esta no nivel {f['nivel']} ({4 * f['nivel']} pontos de atributo).")
        return
    if not await checar_edicao(ctx, f):
        return
    txt = resto[0][0].strip() if len(resto) == 1 else ""
    if txt == "-1" or norm(txt) == "livre":
        novo = -1
    else:
        pv = parse_valor(txt) if txt else None
        if pv is None:
            await ctx.send("uso: `!nivel [personagem] [valor ou +n/-n]` (use `-1` para nivel livre, sem limite de pontos)")
            return
        if f["nivel"] == -1 and pv[0] is not None:
            await ctx.send("essa ficha esta em nivel livre. para sair, informe o nivel direto, ex.: `!nivel 5`.")
            return
        novo = aplicar_valor(f["nivel"], pv)
        if not 1 <= novo <= 20:
            await ctx.send("o nivel vai de 1 a 20 (ou -1 para nivel livre).")
            return
    db.execute("UPDATE fichas SET nivel=? WHERE id=?", (novo, f["id"]))
    db.commit()
    pontos = "sem limite de pontos de atributo" if novo == -1 else f"pontos de atributo: {4 * novo}"
    await ctx.send(f"⭐ **{f['nome']}** — nivel: {fmt_nivel(f['nivel'])} → {fmt_nivel(novo)} ({pontos})")


@bot.command(name="vida")
async def vida(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), sem_resto=True, aridades={0})
    if not f:
        return
    vigor, vontade = maximos(f, get_bonus(f["id"]))
    pc = penalidade(f["corpo"]) if f["corpo"] <= 40 else 0
    pm = penalidade(f["mente"]) if f["mente"] <= 40 else 0
    msg = (f"❤️ **{f['nome']}**\n"
           f"corpo: **{f['corpo']}/{vigor}** (penalidade em todos os atributos: −{pc})\n"
           f"mente: **{f['mente']}/{vontade}** (penalidade extra nos mentais: −{pm})")
    if f["corpo"] == 0:
        msg += "\n💀 corpo em 0: desmaiado ou morto, conforme o contexto."
    if f["mente"] == 0:
        msg += "\n🌀 mente em 0."
    await ctx.send(msg)


def rolar_attr(f, attr, extra_bonus=0, base=None, com_bonus=True):
    """rola d20 + atributo, com bonus de itens/status, penalidades e o bonus do 20 natural.
    extra_bonus: bonus avulso so desta rolagem (ex.: habilidade usada no !d20).
    base/com_bonus: rolagem de habilidade (valor proprio da habilidade, sem bonus de itens/status)."""
    dado = random.randint(1, 20)
    if base is None:
        base = f[attr]
    bonus = get_bonus(f["id"]).get(attr, 0) if com_bonus else 0
    pen = penalidade(f["corpo"]) if f["corpo"] <= 40 else 0
    if attr in MENTAIS and f["mente"] <= 40:
        pen += penalidade(f["mente"])
    extra = max(0, int(base * 0.5)) if dado == 20 else 0
    total = base + dado + bonus + extra_bonus - pen + extra
    return {"dado": dado, "base": base, "bonus": bonus, "habilidade": extra_bonus, "pen": pen,
            "extra": extra, "total": total}


def texto_rolagem(attr, r, nome_hab=None, rotulo_base=None) -> str:
    partes = [f"[{r['dado']}]", f"{r['base']} ({rotulo_base or ATRIBUTOS[attr]})"]
    if r["bonus"]:
        partes.append(f"{r['bonus']:+d} (itens/status)")
    if r.get("habilidade"):
        partes.append(f"{r['habilidade']:+d} ({nome_hab or 'habilidade'})")
    if r["pen"]:
        partes.append(f"−{r['pen']} (penalidades)")
    if r["dado"] == 20:
        partes.append(f"+{r['extra']} (20 natural)")
    return " + ".join(partes).replace("+ −", "− ").replace("+ +", "+ ")


def ficha_por_token(ctx, t):
    """ficha pelo nome (ou comeco do nome) ou pela ordem das suas fichas (#2)."""
    f = get_ficha(ctx.guild.id, t)
    if not f:
        m = re.fullmatch(r"#?(\d+)", t)
        minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
        if m and 1 <= int(m.group(1)) <= len(minhas):
            f = minhas[int(m.group(1)) - 1]
    return f


async def rolar_habilidade(ctx, f, hab, attr):
    """rola o valor proprio da habilidade num atributo: d20 + valor da habilidade
    (porcentagem = % do atributo base da ficha). nao soma bonus de itens/status."""
    if not await checar_edicao(ctx, f):
        return
    bonus_hab = json.loads(hab["bonus"])
    bruto = bonus_hab.get(attr)
    if bruto is None:
        definidos = {k: v for k, v in bonus_hab.items() if k in ATRIBUTOS}
        dica = ("ela define: " + fmt_bonus(definidos)) if definidos else "ela so tem efeito imediato (crp/mnt), nao ha o que rolar"
        await ctx.send(f"**{hab['nome']}** nao tem valor em {ATRIBUTOS[attr]}; {dica}.")
        return
    base = valor_bonus(bruto, f[attr])
    r = rolar_attr(f, attr, base=base, com_bonus=False)
    rotulo = f"{hab['nome']} {ATRIBUTOS[attr]}" + (f" {bruto}" if isinstance(bruto, str) else "")
    aviso = "\n💀 corpo em 0!" if f["corpo"] == 0 else ""
    await ctx.send(f"🎲 **{f['nome']}** — habilidade {hab['nome']} ({ATRIBUTOS[attr]}): "
                   f"{texto_rolagem(attr, r, rotulo_base=rotulo)} = **{r['total']}**{aviso}")


@bot.command(name="d20")
async def d20(ctx, *, raw: str = ""):
    tokens = tokenizar(raw)
    if not tokens:  # d20 puro, sem atributo nem ficha
        dado = random.randint(1, 20)
        await ctx.send(f"🎲 d20: **{dado}**" + (" (20 natural!)" if dado == 20 else ""))
        return
    # [pers] [habilidade] [sigla] (valor da habilidade) ou briga: [pers1] [pers2] ... [sigla]
    if len(tokens) >= 3 and achar_attr(tokens[-1][0]) and not achar_attr(tokens[1][0]):
        if len(tokens) == 3:
            f0 = ficha_por_token(ctx, tokens[0][0])
            hab0 = get_habilidade(f0["id"], tokens[1][0]) if f0 else None
            if hab0 and not get_ficha(ctx.guild.id, tokens[1][0], exato=True):
                await rolar_habilidade(ctx, f0, hab0, achar_attr(tokens[2][0]))
                return
        await briga_de_rolagem(ctx, tokens)
        return
    # [habilidade] [sigla] da sua ficha #1: o nome exato da habilidade tem prioridade sobre nomes de ficha parecidos
    if len(tokens) == 2 and achar_attr(tokens[1][0]) and not achar_attr(tokens[0][0]):
        minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
        if minhas and not get_ficha(ctx.guild.id, tokens[0][0], exato=True):
            hab0 = get_habilidade(minhas[0]["id"], tokens[0][0], exato=True)
            if hab0:
                await rolar_habilidade(ctx, minhas[0], hab0, achar_attr(tokens[1][0]))
                return
    f, resto = await resolver(ctx, tokens, aridades={1, 2})
    if not f:
        return
    if len(resto) == 2 and not achar_attr(resto[0][0]) and achar_attr(resto[1][0]):
        hab = get_habilidade(f["id"], resto[0][0])
        if not hab:
            await ctx.send(msg_nao_tem(f, "habilidade", resto[0][0]))
            return
        await rolar_habilidade(ctx, f, hab, achar_attr(resto[1][0]))
        return
    attr = achar_attr(resto[0][0]) if len(resto) in (1, 2) else None
    if not attr:
        await ctx.send("uso: `!d20` (puro) • `!d20 [personagem] [sigla] [habilidade opcional]` • "
                       "valor da habilidade: `!d20 [personagem] [habilidade] [sigla]` • "
                       "briga: `!d20 [pers1] [pers2] [sigla]`\n"
                       "ex.: `!d20 com`, `!d20 agi cortelinear`, `!d20 gelo int`, `!d20 douma kage pre`")
        return
    if not await checar_edicao(ctx, f):
        return
    extra, nome_hab, nota = 0, None, ""
    if len(resto) == 2:
        hab = get_habilidade(f["id"], resto[1][0])
        if not hab:
            await ctx.send(msg_nao_tem(f, "habilidade", resto[1][0]))
            return
        nome_hab = hab["nome"]
        bonus_hab = valor_bonus(json.loads(hab["bonus"]).get(attr, 0), f[attr])
        if hab["ativa"]:
            nota = f"\n({hab['nome']} ja esta ativa, entao o bonus ja entra em todas as rolagens)"
        elif bonus_hab:
            extra = bonus_hab
        else:
            nota = f"\n({hab['nome']} nao altera {ATRIBUTOS[attr]})"
    r = rolar_attr(f, attr, extra)
    aviso = "\n💀 corpo em 0!" if f["corpo"] == 0 else ""
    await ctx.send(f"🎲 **{f['nome']}** — {ATRIBUTOS[attr]}: {texto_rolagem(attr, r, nome_hab)} = **{r['total']}**{aviso}{nota}")


async def briga_de_rolagem(ctx, tokens):
    """todos os personagens listados rolam o mesmo atributo; maior vence, diferenca de ate 4 e empate."""
    attr = achar_attr(tokens[-1][0])
    minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
    participantes, vistos, erros = [], set(), []
    for t, _ in tokens[:-1]:
        f = get_ficha(ctx.guild.id, t)
        if not f:
            m = re.fullmatch(r"#?(\d+)", t)
            if m and 1 <= int(m.group(1)) <= len(minhas):
                f = minhas[int(m.group(1)) - 1]
        if not f:
            erros.append(msg_ficha_nao_achada(ctx.guild.id, t))
            continue
        if f["id"] not in vistos:
            vistos.add(f["id"])
            participantes.append(f)
    if erros:
        await ctx.send("\n".join(erros))
        return
    if len(participantes) < 2:
        await ctx.send("a briga precisa de pelo menos 2 personagens diferentes. ex.: `!d20 douma kage pre`")
        return
    if not any(pode_editar(ctx, f) for f in participantes):
        await ctx.send("🚫 para iniciar a briga voce precisa ser dono de pelo menos um dos personagens (ou admin).")
        return
    rolagens = [(f, rolar_attr(f, attr)) for f in participantes]
    rolagens.sort(key=lambda x: -x[1]["total"])
    linhas = [f"{i}. **{f['nome']}** — {texto_rolagem(attr, r)} = **{r['total']}**"
              for i, (f, r) in enumerate(rolagens, 1)]
    topo = rolagens[0][1]["total"]
    dif = topo - rolagens[1][1]["total"]
    if dif > 4:
        veredito = f"🏆 **{rolagens[0][0]['nome']}** vence por {dif}."
    elif len(rolagens) == 2:
        veredito = f"🤝 empate (diferenca de {dif}, ate 4): ninguem leva vantagem."
    else:
        empatados = ", ".join(f"**{f['nome']}**" for f, r in rolagens if topo - r["total"] <= 4)
        veredito = f"🤝 empate no topo entre {empatados} (diferenca de ate 4)."
    await enviar_longo(ctx, f"🥊 **briga de {ATRIBUTOS[attr]}**\n" + "\n".join(linhas) + "\n" + veredito)


@bot.command(name="dano")
async def dano(ctx, *, raw: str = ""):
    """calcula o dano: [rolagem de dano] - [rolagem de defesa]; diferenca de ate 4 = sem dano."""
    tokens = tokenizar(raw)
    uso = ("uso: `!dano [rolagem de dano] [res|sab] [rolagem de defesa] [personagem opcional]`\n"
           "ex.: `!dano 20 res 12` (so calcula) ou `!dano 20 res 12 kage` (calcula e tira do corpo do kage)")
    if len(tokens) not in (3, 4):
        await ctx.send(uso)
        return
    if not re.fullmatch(r"-?\d+", tokens[0][0].strip()) or not re.fullmatch(r"-?\d+", tokens[2][0].strip()):
        await ctx.send("a rolagem de dano e a de defesa precisam ser numeros inteiros.\n" + uso)
        return
    ataque, defesa = int(tokens[0][0]), int(tokens[2][0])
    attr = achar_attr(tokens[1][0])
    if attr not in ("resistencia", "sabedoria"):
        await ctx.send("a defesa precisa ser `res` (dano fisico, tira do corpo) ou `sab` (dano mental, tira da mente).")
        return
    campo = "corpo" if attr == "resistencia" else "mente"
    dif = ataque - defesa
    cab = f"⚔️ dano: {ataque} − {defesa} ({ATRIBUTOS[attr]}) = {dif}"
    pontos = dif if dif > 4 else 0
    if dif < 0:
        texto = f"{cab} → sem dano (a defesa venceu por {-dif})."
    elif dif <= 4:
        texto = f"{cab} → empate (diferenca de ate 4): sem dano."
    else:
        texto = f"{cab} → 💥 **{pontos}** ponto(s) de {campo} ({SIG[campo]}) removido(s)."
    if len(tokens) == 4:
        alvo_txt = tokens[3][0]
        alvo = get_ficha(ctx.guild.id, alvo_txt)
        if not alvo:
            minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
            m = re.fullmatch(r"#?(\d+)", alvo_txt)
            if m and 1 <= int(m.group(1)) <= len(minhas):
                alvo = minhas[int(m.group(1)) - 1]
        if not alvo:
            await ctx.send(texto + "\n" + msg_ficha_nao_achada(ctx.guild.id, alvo_txt) + " (nada foi alterado)")
            return
        if pontos == 0:
            texto += f"\n**{alvo['nome']}** nao perde nada."
        elif not pode_editar(ctx, alvo):
            texto += f"\n🚫 so o dono de **{alvo['nome']}** ou um admin pode aplicar o dano; nada foi alterado."
        else:
            antes = alvo[campo]
            novo = max(0, antes - pontos)
            db.execute(f"UPDATE fichas SET {campo}=? WHERE id=?", (novo, alvo["id"]))
            db.commit()
            registrar_alvo(ctx, alvo)
            texto += f"\n**{alvo['nome']}** — {SIG[campo]}: {antes} → {novo}"
            if novo == 0:
                texto += ("\n💀 corpo em 0: desmaiado ou morto, conforme o contexto." if campo == "corpo"
                          else "\n🌀 mente em 0.")
    elif pontos:
        texto += "\n(so calculo. para aplicar, diga o personagem no fim: `!dano 20 res 12 kage`)"
    await ctx.send(texto)


def desempatar(grupo, tentativas=0):
    """quem empatou rola agilidade de novo, repetindo entre os que continuarem empatados."""
    if len(grupo) == 1:
        return grupo
    if tentativas >= 20:
        random.shuffle(grupo)
        return grupo
    for it in grupo:
        it.setdefault("desempates", []).append(rolar_attr(it["f"], "agilidade")["total"])
    grupo.sort(key=lambda i: -i["desempates"][-1])
    resultado, k = [], 0
    while k < len(grupo):
        j = k
        while j < len(grupo) and grupo[j]["desempates"][-1] == grupo[k]["desempates"][-1]:
            j += 1
        resultado.extend(desempatar(grupo[k:j], tentativas + 1))
        k = j
    return resultado


def ordenar_iniciativa(itens):
    itens = sorted(itens, key=lambda i: -i["total"])
    ordenados, i = [], 0
    while i < len(itens):
        j = i
        while j < len(itens) and itens[j]["total"] == itens[i]["total"]:
            j += 1
        ordenados.extend(desempatar(itens[i:j]))
        i = j
    return ordenados


@bot.command(name="iniciativa")
async def iniciativa(ctx, *, raw: str = ""):
    nomes = [t.strip(",") for t, _ in tokenizar(raw) if t.strip(",")]
    if not nomes:
        await ctx.send("uso: `!iniciativa [personagem1] [personagem2] ...` (nome ou ordem `#2`; aspas se o nome tiver espacos)")
        return
    minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
    itens, vistos, avisos = [], set(), []
    for n in nomes:
        f = get_ficha(ctx.guild.id, n)
        if not f:
            m = re.fullmatch(r"#?(\d+)", n)
            if m and 1 <= int(m.group(1)) <= len(minhas):
                f = minhas[int(m.group(1)) - 1]
        if not f:
            avisos.append(msg_ficha_nao_achada(ctx.guild.id, n))
            continue
        if not pode_editar(ctx, f):
            avisos.append(f"🚫 voce nao pode rolar a iniciativa de **{f['nome']}**.")
            continue
        if f["id"] in vistos:
            continue
        vistos.add(f["id"])
        r = rolar_attr(f, "agilidade")
        itens.append({"f": f, "r": r, "total": r["total"]})
    if not itens:
        await enviar_longo(ctx, "\n".join(avisos))
        return
    linhas = []
    for pos, it in enumerate(ordenar_iniciativa(itens), 1):
        f, r = it["f"], it["r"]
        desempate = f" (desempate: {' → '.join(str(x) for x in it['desempates'])})" if it.get("desempates") else ""
        morto = " 💀" if f["corpo"] == 0 else ""
        linhas.append(f"**{pos}.** **{f['nome']}**{morto} — {texto_rolagem('agilidade', r)} = **{r['total']}**{desempate}")
    texto = "🎯 **iniciativa** (do maior para o menor)\n" + "\n".join(linhas)
    if avisos:
        texto += "\n\n" + "\n".join(avisos)
    await enviar_longo(ctx, texto)


# inventario
@bot.command(name="inventario")
async def inventario(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), sem_resto=True, aridades={0})
    if not f:
        return
    rows = db.execute("SELECT * FROM itens WHERE ficha_id=? ORDER BY nome", (f["id"],)).fetchall()
    if not rows:
        await ctx.send(f"🎒 o inventario de **{f['nome']}** esta vazio.")
        return
    linhas = []
    for r in rows:
        bonus = fmt_bonus(json.loads(r["bonus"]))
        venda = f" · 💱 {r['vendaveis']} disponível(is) para venda" if r["vendaveis"] else ""
        if r["tipo"] == "consumivel":
            linhas.append(f"🍖 **{r['nome']}** · consumível · quantidade: {r['quantidade']}\n"
                          f"   Efeito: {bonus}{venda}")
        else:
            marca = "🟢" if r["equipado"] > 0 else "⚪"
            linhas.append(f"{marca} **{r['nome']}** · normal · quantidade: {r['quantidade']} "
                          f"(equipados: {r['equipado']})\n   Bônus: {bonus}{venda}")
    await enviar_longo(
        ctx,
        f"🎒 **Inventário · {f['nome']}**\n"
        f"Saldo: **{f['moedas']}** 💰\n\n"
        "🟢 equipado · ⚪ guardado · 🍖 consumível · 💱 item que pode ser vendido\n\n"
        + "\n\n".join(linhas)
    )


@bot.command(name="pegar")
async def pegar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={2, 3, 4, 5})
    if not f:
        return
    n = len(resto)
    if n not in (2, 3, 4, 5):
        await ctx.send("uso: `!pegar [personagem] [item] [quantidade] [tipo] [+sigla,-sigla] [valores]`\n"
                       "tipo (opcional): normal ou consumível.\n"
                       "ex.: `!pegar \"espada longa\" 1 normal +for,-agi 3,1`\n"
                       "ex.: `!pegar pocao 3 consumível +crp 20`")
        return
    nome_item = resto[0][0].strip()
    erro_nome = validar_nome(nome_item)
    if erro_nome:
        await ctx.send(erro_nome)
        return
    try:
        qtd = int(resto[1][0])
        if qtd < 1:
            raise ValueError
    except ValueError:
        await ctx.send("a quantidade precisa ser um numero inteiro maior que 0.")
        return
    tipo, attrs, vals = None, None, None
    if n == 3:
        tipo_txt = resto[2][0]
    elif n == 4:
        attrs, vals = resto[2][0], resto[3][0]
    elif n == 5:
        tipo_txt = resto[2][0]
        attrs, vals = resto[3][0], resto[4][0]
    if n in (3, 5):
        tipo = TIPOS_ITEM.get(norm(tipo_txt))
        if not tipo:
            await ctx.send("o tipo precisa ser `normal` ou `consumível`.")
            return
    existente = get_item(f["id"], nome_item, exato=True)
    if existente and tipo and tipo != existente["tipo"]:
        await ctx.send(f"**{existente['nome']}** ja existe como {nome_tipo_item(existente['tipo'])}. "
                       "descarte o item e crie de novo para mudar o tipo.")
        return
    tipo_final = tipo or (existente["tipo"] if existente else "estatico")
    bonus = None
    if attrs is not None:
        permitidos = set(ATRIBUTOS) if tipo_final == "estatico" else {"corpo", "mente"}
        bonus, erro = parse_bonus(attrs, vals, permitidos)
        if erro:
            if tipo_final == "consumivel":
                erro += " (consumível afeta apenas **crp** ou **mnt**.)"
            await ctx.send(erro)
            return
    if existente:
        nova = existente["quantidade"] + qtd
        db.execute("UPDATE itens SET quantidade=?, bonus=? WHERE id=?",
                   (nova, json.dumps(bonus) if bonus is not None else existente["bonus"], existente["id"]))
        msg = f"➕ **{existente['nome']}** agora esta com x{nova} no inventario de **{f['nome']}**."
    else:
        db.execute("INSERT INTO itens (ficha_id, nome, quantidade, bonus, tipo) VALUES (?, ?, ?, ?, ?)",
                   (f["id"], nome_item, qtd, json.dumps(bonus or {}), tipo_final))
        msg = (f"🎒 **{nome_item}** x{qtd} [{nome_tipo_item(tipo_final)}] "
               f"({fmt_bonus(bonus or {})}) adicionado a **{f['nome']}**.")
    db.commit()
    await ctx.send(msg)


@bot.command(name="descartar")
async def descartar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1, 2})
    if not f:
        return
    if len(resto) not in (1, 2):
        await ctx.send("uso: `!descartar [personagem] [item] [quantidade opcional]`")
        return
    item = get_item(f["id"], resto[0][0], seguro=True)
    if not item:
        await ctx.send(msg_nao_tem(f, "item", resto[0][0]))
        return
    qtd = None
    if len(resto) == 2:
        try:
            qtd = int(resto[1][0])
            if qtd < 1:
                raise ValueError
        except ValueError:
            await ctx.send("a quantidade precisa ser um numero inteiro maior que 0.")
            return
    if qtd is not None and qtd < item["quantidade"]:
        nova = item["quantidade"] - qtd
        db.execute("UPDATE itens SET quantidade=?, equipado=?, vendaveis=? WHERE id=?",
                   (nova, min(item["equipado"], nova), min(item["vendaveis"], nova), item["id"]))
        msg = f"🗑️ **{f['nome']}** descartou {qtd}x **{item['nome']}** (restam {nova})."
    else:
        db.execute("DELETE FROM itens WHERE id=?", (item["id"],))
        msg = f"🗑️ item **{item['nome']}** removido de **{f['nome']}**."
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(msg)


@bot.command(name="equipar")
async def equipar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!equipar [personagem] [item]` (cada uso equipa mais uma unidade)")
        return
    item = get_item(f["id"], resto[0][0])
    if not item:
        await ctx.send(msg_nao_tem(f, "item", resto[0][0]))
        return
    if item["tipo"] != "estatico":
        await ctx.send(f"**{item['nome']}** e consumível e nao se equipa. use `!usaritem`.")
        return
    if item["equipado"] >= item["quantidade"]:
        await ctx.send(f"todas as {item['quantidade']} unidade(s) de **{item['nome']}** ja estao equipadas.")
        return
    novo = item["equipado"] + 1
    db.execute("UPDATE itens SET equipado=? WHERE id=?", (novo, item["id"]))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"⚔️ **{f['nome']}** equipou **{item['nome']}** ({novo}/{item['quantidade']} equipadas) — "
                   f"{fmt_bonus(json.loads(item['bonus']))} por unidade.")


@bot.command(name="desequipar")
async def desequipar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!desequipar [personagem] [item]` (cada uso tira uma unidade)")
        return
    item = get_item(f["id"], resto[0][0])
    if not item:
        await ctx.send(msg_nao_tem(f, "item", resto[0][0]))
        return
    if item["equipado"] < 1:
        await ctx.send(f"**{item['nome']}** nao esta equipado.")
        return
    novo = item["equipado"] - 1
    db.execute("UPDATE itens SET equipado=? WHERE id=?", (novo, item["id"]))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"🎒 **{f['nome']}** desequipou **{item['nome']}** ({novo}/{item['quantidade']} equipadas).")


@bot.command(name="equipados")
async def equipados(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), sem_resto=True, aridades={0})
    if not f:
        return
    rows = db.execute("SELECT * FROM itens WHERE ficha_id=? AND equipado>0 ORDER BY nome", (f["id"],)).fetchall()
    if not rows:
        await ctx.send(f"**{f['nome']}** nao tem itens equipados.")
        return
    linhas = [f"⚔️ **{r['nome']}** {r['equipado']}/{r['quantidade']} equipada(s) — "
              f"{fmt_bonus(json.loads(r['bonus']))} por unidade" for r in rows]
    await enviar_longo(ctx, f"**itens equipados de {f['nome']}**\n" + "\n".join(linhas)
                       + f"\n\n**total:** {fmt_bonus(get_bonus(f['id']))} (inclui status estatico)")


async def aplicar_item_consumivel(ctx, origem, item, alvo):
    bonus = json.loads(item["bonus"])
    vigor, vontade = maximos(alvo, get_bonus(alvo["id"]))
    valores = {"corpo": alvo["corpo"], "mente": alvo["mente"]}
    limites = {"corpo": vigor, "mente": vontade}
    linhas = []
    for k, v in bonus.items():
        antes = valores[k]
        valores[k] = max(0, min(limites[k], antes + valor_bonus(v, limites[k])))
        linhas.append(f"{SIG[k]}: {antes} → {valores[k]}")
    db.execute("UPDATE fichas SET corpo=?, mente=? WHERE id=?", (valores["corpo"], valores["mente"], alvo["id"]))
    if item["quantidade"] > 1:
        db.execute("UPDATE itens SET quantidade=quantidade-1, vendaveis=MIN(vendaveis, quantidade-1) WHERE id=?",
                   (item["id"],))
        sobra = f"restam {item['quantidade'] - 1}"
    else:
        db.execute("DELETE FROM itens WHERE id=?", (item["id"],))
        sobra = "acabaram"
    db.commit()
    normalizar_vitais(alvo["id"])
    registrar_alvo(ctx, origem)
    registrar_alvo(ctx, alvo)
    efeito = "\n".join(linhas) if linhas else "sem efeito mecanico."
    quem = f" em **{alvo['nome']}**" if alvo["id"] != origem["id"] else ""
    await ctx.send(f"🍖 **{origem['nome']}** usou **{item['nome']}**{quem} ({sobra}).\n{efeito}")


@bot.command(name="usaritem")
async def usaritem(ctx, *, raw: str = ""):
    run = run_do_usuario(ctx.guild.id, ctx.author.id)
    comando_raw, alvo_ref = separar_alvo_dungeon(run, raw, inimigos=False) if run else (raw, None)
    if alvo_ref and (alvo_ref[0] != "j" or alvo_ref[1] not in run["jogadores"]):
        await ctx.send("itens so podem ser usados em fichas do seu grupo na dungeon.")
        return
    f, resto = await resolver(ctx, tokenizar(comando_raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!usaritem [personagem] [item] [alvo opcional do grupo]`")
        return
    item = get_item(f["id"], resto[0][0])
    if not item:
        await ctx.send(msg_nao_tem(f, "item", resto[0][0]))
        return
    if item["tipo"] != "consumivel":
        await ctx.send(f"**{item['nome']}** e normal. use `!equipar`.")
        return
    alvo = get_ficha_id(alvo_ref[1]) if alvo_ref else f
    if alvo is None:
        await ctx.send("nao encontrei a ficha-alvo.")
        return
    if alvo["id"] != f["id"] and (not run or alvo["id"] not in run["jogadores"]):
        await ctx.send("itens so podem ser usados em fichas do seu grupo na dungeon.")
        return
    await aplicar_item_consumivel(ctx, f, item, alvo)


# transferencias, moedas e loja
def separar_qtd(resto):
    """[item com espacos...] [qtd|tudo] -> (nome, qtd, explicita). qtd 'tudo' = None. qtd invalida -> (None, 0, True)."""
    textos = [t for t, _ in resto]
    qtd, explicita = 1, False
    if len(textos) >= 2 and (textos[-1].isdigit() or norm(textos[-1]) == "tudo"):
        explicita = True
        if norm(textos[-1]) == "tudo":
            qtd = None
        else:
            qtd = int(textos[-1])
            if qtd < 1:
                return None, 0, True
        textos = textos[:-1]
    nome = " ".join(textos).strip()
    return (nome or None), qtd, explicita


def tirar_unidades(item, qtd: int, vendaveis_saindo=None) -> int:
    """tira qtd unidades do item (apaga a linha se acabar). as vendaveis saem primeiro.
    retorna quantas unidades vendaveis sairam."""
    nova = item["quantidade"] - qtd
    vend_sai = min(qtd, item["vendaveis"]) if vendaveis_saindo is None else vendaveis_saindo
    if nova <= 0:
        db.execute("DELETE FROM itens WHERE id=?", (item["id"],))
    else:
        db.execute("UPDATE itens SET quantidade=?, equipado=?, vendaveis=? WHERE id=?",
                   (nova, min(item["equipado"], nova), min(max(0, item["vendaveis"] - vend_sai), nova), item["id"]))
    return vend_sai


def mesmo_item(a, b_tipo, b_bonus) -> bool:
    return a["tipo"] == b_tipo and json.loads(a["bonus"]) == b_bonus


@bot.command(name="transferir")
async def transferir(ctx, *, raw: str = ""):
    uso = ("uso: `!transferir [personagem] [item] [qtd|tudo] [destino]`\n"
           "ex.: `!transferir \"Pocao de Cura\" 2 Douma`")
    tokens = tokenizar(raw)
    if len(tokens) < 2:
        await ctx.send(uso)
        return
    destino_txt = tokens[-1][0].strip().strip(ASPAS).strip()
    dest = ficha_por_token(ctx, destino_txt)
    if not dest:
        await ctx.send(msg_ficha_nao_achada(ctx.guild.id, destino_txt))
        return
    f, resto = await resolver(ctx, tokens[:-1], editar=True, aridades=lambda n: n >= 1)
    if not f:
        return
    nome_txt, qtd, _ = separar_qtd(resto)
    if not nome_txt:
        await ctx.send(uso)
        return
    if dest["id"] == f["id"]:
        await ctx.send("a origem e o destino sao a mesma ficha.")
        return
    ro, rd = run_da_ficha(f["id"]), run_da_ficha(dest["id"])
    if ro or rd:
        if ro is not rd:
            await ctx.send("⚔️ dentro de uma dungeon so da para trocar com quem esta no mesmo grupo.")
            return
        if ro["estado"] == "combate":
            await ctx.send("⚔️ em combate nao da para trocar nada; esperem o acampamento.")
            return
    registrar_alvo(ctx, dest)

    item = get_item(f["id"], nome_txt)
    if not item:
        await ctx.send(msg_nao_tem(f, "item", nome_txt))
        return
    qtd = item["quantidade"] if qtd is None else qtd
    if qtd > item["quantidade"]:
        await ctx.send(f"**{f['nome']}** tem so {item['quantidade']}x **{item['nome']}**.")
        return
    existente = get_item(dest["id"], item["nome"], exato=True)
    bonus = json.loads(item["bonus"])
    if existente and not mesmo_item(existente, item["tipo"], bonus):
        await ctx.send(f"**{dest['nome']}** ja tem um item chamado **{existente['nome']}** com outro efeito. "
                       "renomeie um dos dois com `!renomearitem` antes de transferir.")
        return
    equip_antes = item["equipado"]
    vend = tirar_unidades(item, qtd)
    if existente:
        db.execute("UPDATE itens SET quantidade=quantidade+?, vendaveis=vendaveis+? WHERE id=?",
                   (qtd, vend, existente["id"]))
    else:
        db.execute("INSERT INTO itens (ficha_id, nome, quantidade, bonus, tipo, vendaveis) VALUES (?, ?, ?, ?, ?, ?)",
                   (dest["id"], item["nome"], qtd, item["bonus"], item["tipo"], vend))
    db.commit()
    normalizar_vitais(f["id"])
    normalizar_vitais(dest["id"])
    msg = f"📦 **{f['nome']}** entregou {qtd}x **{item['nome']}** para **{dest['nome']}**."
    sobra = item["quantidade"] - qtd
    if equip_antes > sobra:
        msg += f"\n(desequipou {equip_antes - max(0, sobra)} unidade(s) que estavam em uso.)"
    await ctx.send(msg)


@bot.command(name="pix")
async def pix(ctx, *, raw: str = ""):
    uso = "uso: `!pix [personagem] [moedas] [destino]` • ex.: `!pix Kage 50 Douma`"
    tokens = tokenizar(raw)
    if len(tokens) < 2 or not re.fullmatch(r"\d+", tokens[-2][0]):
        await ctx.send(uso)
        return
    valor = int(tokens[-2][0])
    destino_txt = tokens[-1][0].strip().strip(ASPAS).strip()
    dest = ficha_por_token(ctx, destino_txt)
    if not dest:
        await ctx.send(msg_ficha_nao_achada(ctx.guild.id, destino_txt))
        return
    f, resto = await resolver(ctx, tokens[:-2], editar=True, aridades={0})
    if not f:
        return
    if resto:
        await ctx.send(uso)
        return
    if dest["id"] == f["id"]:
        await ctx.send("a origem e o destino sao a mesma ficha.")
        return
    ro, rd = run_da_ficha(f["id"]), run_da_ficha(dest["id"])
    if ro or rd:
        if ro is not rd:
            await ctx.send("⚔️ dentro de uma dungeon so da para trocar com quem esta no mesmo grupo.")
            return
        if ro["estado"] == "combate":
            await ctx.send("⚔️ em combate nao da para trocar nada; esperem o acampamento.")
            return
    if valor < 1:
        await ctx.send("a quantidade de moedas precisa ser maior que zero.")
        return
    if f["moedas"] < valor:
        await ctx.send(f"💰 **{f['nome']}** tem so **{f['moedas']}** moeda(s).")
        return
    registrar_alvo(ctx, dest)
    db.execute("UPDATE fichas SET moedas=moedas-? WHERE id=?", (valor, f["id"]))
    db.execute("UPDATE fichas SET moedas=moedas+? WHERE id=?", (valor, dest["id"]))
    db.commit()
    await ctx.send(f"💸 **{f['nome']}** enviou **{valor}** moeda(s) para **{dest['nome']}**. "
                   f"({f['nome']}: {f['moedas']} → {f['moedas'] - valor} • "
                   f"{dest['nome']}: {dest['moedas']} → {dest['moedas'] + valor})")


@bot.command(name="saldo", aliases=["moedas"])
async def moedas(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), sem_resto=True, aridades={0})
    if not f:
        return
    await ctx.send(f"💰 **{f['nome']}** tem **{f['moedas']}** moeda(s).")


@bot.command(name="darmoedas")
async def darmoedas(ctx, *, raw: str = ""):
    if not eh_admin(ctx):
        await ctx.send("🚫 so admins podem dar ou tirar moedas.")
        return
    f, resto = await resolver(ctx, tokenizar(raw), numero_e_valor=True, aridades={1})
    if not f:
        return
    if len(resto) != 1 or not re.fullmatch(r"[+-]?\d+", resto[0][0].strip()):
        await ctx.send("uso: `!darmoedas [personagem] [quantidade]` (use `-50` para tirar)")
        return
    registrar_alvo(ctx, f)
    novo = max(0, f["moedas"] + int(resto[0][0]))
    db.execute("UPDATE fichas SET moedas=? WHERE id=?", (novo, f["id"]))
    db.commit()
    await ctx.send(f"💰 **{f['nome']}** — moedas: {f['moedas']} → {novo}")


def texto_loja() -> str:
    linhas = [
        "🏪 **Loja**",
        "Compre com `!comprar [personagem] [item ou habilidade] [quantidade]`.",
        "Venda com `!vender [personagem] [item] [quantidade ou tudo]` · a loja paga metade do preço.",
        "",
        "**Consumíveis** · usados para recuperar corpo ou mente",
    ]
    for nome in CONSUMIVEIS:
        c = CATALOGO[nome]
        linhas.append(f"• **{nome}** — {c['preco']} 💰 ({fmt_bonus(c['bonus'])})")
    linhas += ["", "**Equipamentos** · o bônus aumenta conforme a raridade"]
    for base, gen, attrs in EQUIPAMENTOS:
        precos = []
        for t in range(3):
            c = CATALOGO[nome_equip(base, gen, t)]
            nivel = f" · nível mínimo {c['nivel']}" if c["nivel"] > 1 else ""
            precos.append(f"  • {TIERS[t][0 if gen == 'm' else 1]} · {c['preco']} 💰"
                          f" · bônus: {fmt_bonus(c['bonus'])}{nivel}")
        linhas.append(f"**{base}**\n" + "\n".join(precos))
    linhas.append(f"Equipamentos lendários só aparecem nas dungeons. "
                  f"A loja paga {PRECO_TIER[3] // 2} 💰 por eles.")
    linhas += ["", "**Habilidades** · aprenda com `!comprar [personagem] [habilidade]`"]
    if HABILIDADES_LOJA:
        for habilidade in HABILIDADES_LOJA:
            linhas.append(
                f"• **{habilidade['nome']}** · {habilidade['preco']} 💰\n"
                f"  Efeito: {fmt_bonus(habilidade['bonus'])} · "
                f"Duração: {fmt_duracao_hab(habilidade['bonus'], habilidade['duracao'])} · "
                f"Tipo: {nome_tipo_habilidade(habilidade['modo'])}"
            )
    else:
        linhas.append("• nenhuma habilidade disponivel no momento.")
    return "\n".join(linhas)


@bot.command(name="loja")
async def loja(ctx):
    await enviar_longo(ctx, texto_loja())


@bot.command(name="comprar")
async def comprar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades=lambda n: n >= 1)
    if not f:
        return
    nome_txt, qtd, _ = separar_qtd(resto)
    if not nome_txt:
        await ctx.send("uso: `!comprar [personagem] [item] [quantidade]` ou "
                       "`!comprar [personagem] [habilidade]` — veja a lista com `!loja`.")
        return
    if run_da_ficha(f["id"]):
        await ctx.send("🏰 nao ha loja dentro da dungeon. procure mercadores nos eventos do acampamento.")
        return
    a_venda = [c for c in CATALOGO.values() if c["na_loja"]]
    alvo = achar_nome(a_venda, nome_txt)
    if not alvo:
        cands = candidatos_nome(a_venda, nome_txt)
        if len(cands) > 1:
            await ctx.send("qual deles? " + ", ".join(f"**{c['nome']}**" for c in cands[:8]))
            return
        habilidade = achar_nome(HABILIDADES_LOJA, nome_txt)
        if not habilidade:
            cands = candidatos_nome(HABILIDADES_LOJA, nome_txt)
            if len(cands) > 1:
                await ctx.send("qual delas? " + ", ".join(f"**{h['nome']}**" for h in cands[:8]))
            else:
                await ctx.send(f"a loja nao vende **{nome_txt}**. veja a lista com `!loja`.")
            return
        if qtd is None:
            await ctx.send("habilidades sao compradas uma por vez.")
            return
        if qtd != 1:
            await ctx.send("habilidades sao compradas uma por vez; nao informe quantidade.")
            return
        existente = get_habilidade(f["id"], habilidade["nome"], exato=True)
        if existente:
            await ctx.send(f"✨ **{f['nome']}** ja conhece **{habilidade['nome']}**.")
            return
        if f["moedas"] < habilidade["preco"]:
            await ctx.send(f"💰 **{habilidade['nome']}** custa {habilidade['preco']} e "
                           f"**{f['nome']}** tem so {f['moedas']} moeda(s).")
            return
        comprada = False
        with db:
            atualizacao = db.execute(
                "UPDATE fichas SET moedas=moedas-? WHERE id=? AND moedas>=?",
                (habilidade["preco"], f["id"], habilidade["preco"]),
            )
            if atualizacao.rowcount == 1:
                db.execute(
                    "INSERT INTO habilidades (ficha_id, nome, bonus, duracao, modo) VALUES (?, ?, ?, ?, ?)",
                    (f["id"], habilidade["nome"], json.dumps(habilidade["bonus"]),
                     habilidade["duracao"], habilidade["modo"]),
                )
                comprada = True
        if not comprada:
            await ctx.send(f"💰 **{f['nome']}** nao tem moedas suficientes para **{habilidade['nome']}**.")
            return
        await ctx.send(f"📖 **{f['nome']}** aprendeu **{habilidade['nome']}** "
                       f"({fmt_bonus(habilidade['bonus'])} • tipo {nome_tipo_habilidade(habilidade['modo'])}) "
                       f"por {habilidade['preco']} 💰. restam {f['moedas'] - habilidade['preco']} moeda(s).")
        return
    if qtd is None:
        await ctx.send("uso: `!comprar [personagem] [item] [quantidade]` — veja os itens com `!loja`.")
        return
    nv = min(20, nivel_efetivo(f))
    if nv < alvo["nivel"]:
        await ctx.send(f"🔒 **{alvo['nome']}** so e vendido a partir do nivel {alvo['nivel']} (**{f['nome']}** e nivel {nv}).")
        return
    total = alvo["preco"] * qtd
    if f["moedas"] < total:
        await ctx.send(f"💰 {qtd}x **{alvo['nome']}** custa {total} e **{f['nome']}** tem so {f['moedas']}.")
        return
    existente = get_item(f["id"], alvo["nome"], exato=True)
    if existente and not mesmo_item(existente, alvo["tipo"], alvo["bonus"]):
        await ctx.send(f"**{f['nome']}** ja tem um item chamado **{existente['nome']}** com outro efeito. "
                       "renomeie o seu com `!renomearitem` antes de comprar.")
        return
    db.execute("UPDATE fichas SET moedas=moedas-? WHERE id=?", (total, f["id"]))
    dar_item(f["id"], alvo, qtd)
    await ctx.send(f"🛒 **{f['nome']}** comprou {qtd}x **{alvo['nome']}** ({fmt_bonus(alvo['bonus'])}) por {total} 💰. "
                   f"restam {f['moedas'] - total} moeda(s).")


@bot.command(name="vender")
async def vender(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades=lambda n: n >= 1)
    if not f:
        return
    nome_txt, qtd, _ = separar_qtd(resto)
    if not nome_txt:
        await ctx.send("uso: `!vender [personagem] [item] [quantidade|tudo]`")
        return
    if run_da_ficha(f["id"]):
        await ctx.send("🏰 nao ha loja dentro da dungeon.")
        return
    item = get_item(f["id"], nome_txt)
    if not item:
        await ctx.send(msg_nao_tem(f, "item", nome_txt))
        return
    cat = CATALOGO_NORM.get(norm(item["nome"]))
    if not cat or not mesmo_item(item, cat["tipo"], cat["bonus"]):
        await ctx.send(f"a loja nao compra **{item['nome']}**: so itens do catalogo (`!loja`) e os achados nas dungeons.")
        return
    if item["vendaveis"] < 1:
        await ctx.send(f"**{item['nome']}** de **{f['nome']}** foi criado a mao (`!pegar`) e nao pode ser vendido. "
                       "so vende o que foi comprado ou achado nas dungeons.")
        return
    qtd = item["vendaveis"] if qtd is None else qtd
    if qtd > item["vendaveis"]:
        await ctx.send(f"**{f['nome']}** so pode vender {item['vendaveis']}x **{item['nome']}**.")
        return
    valor = (cat["preco"] // 2) * qtd
    tirar_unidades(item, qtd, vendaveis_saindo=qtd)
    db.execute("UPDATE fichas SET moedas=moedas+? WHERE id=?", (valor, f["id"]))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"💱 **{f['nome']}** vendeu {qtd}x **{item['nome']}** por {valor} 💰 "
                   f"(agora tem {f['moedas'] + valor}).")


# renomear (item, status, nota)
async def _renomear(ctx, raw, tabela, coluna, getter, rotulo):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={2})
    if not f:
        return
    if len(resto) != 2:
        await ctx.send(f"uso: `!renomear{rotulo} [personagem] [{rotulo}] [novo nome]`")
        return
    atual = getter(f["id"], resto[0][0])
    if not atual:
        await ctx.send(msg_nao_tem(f, rotulo, resto[0][0]))
        return
    novo = resto[1][0].strip()
    erro = validar_nome(novo)
    if erro:
        await ctx.send(erro)
        return
    outro = getter(f["id"], novo, exato=True)
    if outro and outro["id"] != atual["id"]:
        await ctx.send(f"**{f['nome']}** ja tem {rotulo} chamado **{novo}**.")
        return
    db.execute(f"UPDATE {tabela} SET {coluna}=? WHERE id=?", (novo, atual["id"]))
    db.commit()
    await ctx.send(f"✏️ {rotulo} **{atual[coluna]}** agora se chama **{novo}**.")


@bot.command(name="renomearitem")
async def renomearitem(ctx, *, raw: str = ""):
    await _renomear(ctx, raw, "itens", "nome", get_item, "item")


@bot.command(name="renomearstatus")
async def renomearstatus(ctx, *, raw: str = ""):
    await _renomear(ctx, raw, "status", "nome", get_status, "status")


@bot.command(name="renomearnota")
async def renomearnota(ctx, *, raw: str = ""):
    await _renomear(ctx, raw, "notas", "titulo", get_nota, "nota")


# status
@bot.command(name="addstatus")
async def addstatus(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={4, 5})
    if not f:
        return
    if len(resto) not in (4, 5):
        await ctx.send("uso: `!addstatus [personagem] [nome] [dinamico|estatico] [+sigla,-sigla] [valores] [turnos opcional]`\n"
                       "ex.: `!addstatus veneno dinamico -crp 3 4` (perde 3 de corpo por turno, 4 turnos)\n"
                       "ex.: `!addstatus bencao estatico +for,+com 2,1`")
        return
    nome_status = resto[0][0].strip()
    erro_nome = validar_nome(nome_status)
    if erro_nome:
        await ctx.send(erro_nome)
        return
    tipo = {"dinamico": "dinamico", "estatico": "estatico"}.get(norm(resto[1][0]))
    if not tipo:
        await ctx.send("o tipo de efeito precisa ser `dinamico` ou `estatico`.")
        return
    permitidos = {"corpo", "mente"} if tipo == "dinamico" else set(ATRIBUTOS)
    bonus, erro = parse_bonus(resto[2][0], resto[3][0], permitidos)
    if erro:
        if tipo == "dinamico":
            erro += " (dinamico afeta apenas **crp** ou **mnt**.)"
        await ctx.send(erro)
        return
    duracao = None
    if len(resto) == 5:
        try:
            duracao = int(resto[4][0])
            if duracao < 1:
                raise ValueError
        except ValueError:
            await ctx.send("a duracao precisa ser um numero de turnos maior que 0.")
            return
    if get_status(f["id"], nome_status, exato=True):
        await ctx.send(f"**{f['nome']}** ja tem o status **{nome_status}**.")
        return
    db.execute("INSERT INTO status (ficha_id, nome, tipo, bonus, duracao) VALUES (?, ?, ?, ?, ?)",
               (f["id"], nome_status, tipo, json.dumps(bonus), duracao))
    db.commit()
    normalizar_vitais(f["id"])
    dur = f" por {duracao} turno(s)" if duracao else " ate ser removido"
    await ctx.send(f"🧪 **{f['nome']}** recebeu **{nome_status}** ({tipo}): {fmt_bonus(bonus, tipo == 'dinamico')}{dur}.")


@bot.command(name="status")
async def status(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), sem_resto=True, aridades={0})
    if not f:
        return
    rows = db.execute("SELECT * FROM status WHERE ficha_id=? ORDER BY id", (f["id"],)).fetchall()
    if not rows:
        await ctx.send(f"**{f['nome']}** nao tem nenhum status ativo.")
        return
    linhas = []
    for r in rows:
        dur = f"{r['duracao']} turno(s) restante(s)" if r["duracao"] else "duração indefinida"
        tipo = "efeito por turno" if r["tipo"] == "dinamico" else "bônus de atributo"
        linhas.append(f"🧪 **{r['nome']}** · {tipo}\n"
                      f"   Efeito: {fmt_bonus(json.loads(r['bonus']), r['tipo'] == 'dinamico')} · {dur}")
    await enviar_longo(
        ctx,
        f"🛡️ **Status ativos · {f['nome']}** ({len(rows)})\n\n" + "\n\n".join(linhas)
    )


@bot.command(name="rmstatus")
async def rmstatus(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={0, 1})
    if not f:
        return
    rows = db.execute("SELECT * FROM status WHERE ficha_id=?", (f["id"],)).fetchall()
    if not rows:
        await ctx.send(f"**{f['nome']}** nao tem status ativo.")
        return
    if not resto:
        if len(rows) == 1:
            alvo = rows[0]
        else:
            nomes = ", ".join(r["nome"] for r in rows)
            await ctx.send(f"qual status remover? ativos: {nomes}. uso: `!rmstatus [personagem] [status]`")
            return
    else:
        alvo = get_status(f["id"], resto[0][0], seguro=True)
        if not alvo:
            await ctx.send(msg_nao_tem(f, "status", resto[0][0]))
            return
    db.execute("DELETE FROM status WHERE id=?", (alvo["id"],))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"✅ status **{alvo['nome']}** removido de **{f['nome']}**.")


# turno
def processar_turno(f) -> list:
    """aplica status dinamico, reduz duracoes e remove status vencidos."""
    linhas = []
    bonus = get_bonus(f["id"])
    vigor, vontade = maximos(f, bonus)
    valores = {"corpo": f["corpo"], "mente": f["mente"]}
    limites = {"corpo": vigor, "mente": vontade}
    inicio = dict(valores)
    sts = db.execute("SELECT * FROM status WHERE ficha_id=? ORDER BY id", (f["id"],)).fetchall()
    for s in sts:
        if s["tipo"] == "dinamico":
            for k, v in json.loads(s["bonus"]).items():
                valores[k] = max(0, min(limites[k], valores[k] + valor_bonus(v, limites[k])))
            linhas.append(f"🧪 {s['nome']}: {fmt_bonus(json.loads(s['bonus']), True)}")
        if s["duracao"] is not None:
            nova = s["duracao"] - 1
            if nova <= 0:
                db.execute("DELETE FROM status WHERE id=?", (s["id"],))
                linhas.append(f"⌛ {s['nome']} acabou.")
            else:
                db.execute("UPDATE status SET duracao=? WHERE id=?", (nova, s["id"]))
    for h in db.execute("SELECT * FROM habilidades WHERE ficha_id=? AND ativa=1 ORDER BY id", (f["id"],)).fetchall():
        if h["restam"] is not None:
            nova = h["restam"] - 1
            if nova <= 0:
                db.execute("UPDATE habilidades SET ativa=0, restam=NULL WHERE id=?", (h["id"],))
                linhas.append(f"⌛ {h['nome']} acabou.")
            else:
                db.execute("UPDATE habilidades SET restam=? WHERE id=?", (nova, h["id"]))
    db.execute("UPDATE fichas SET corpo=?, mente=? WHERE id=?", (valores["corpo"], valores["mente"], f["id"]))
    db.commit()
    normalizar_vitais(f["id"])
    novo = get_ficha_id(f["id"])
    if inicio["corpo"] != novo["corpo"]:
        linhas.append(f"corpo: {inicio['corpo']} → {novo['corpo']}")
    if inicio["mente"] != novo["mente"]:
        linhas.append(f"mente: {inicio['mente']} → {novo['mente']}")
    if novo["corpo"] == 0:
        linhas.append("💀 corpo em 0: desmaiado ou morto, conforme o contexto.")
    if novo["mente"] == 0:
        linhas.append("🌀 mente em 0.")
    return linhas or ["sem efeitos neste turno."]


@bot.command(name="turno")
async def turno(ctx, *, raw: str = ""):
    nomes = [n.strip().strip(ASPAS).strip() for n in raw.split(",") if n.strip()]
    if nomes:
        alvos = []
        minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
        for n in nomes:
            f = get_ficha(ctx.guild.id, n)
            if not f:
                m = re.fullmatch(r"#?(\d+)", n)
                if m and 1 <= int(m.group(1)) <= len(minhas):
                    f = minhas[int(m.group(1)) - 1]
            if not f:
                await ctx.send(msg_ficha_nao_achada(ctx.guild.id, n))
                continue
            if not pode_editar(ctx, f):
                await ctx.send(f"🚫 voce nao pode passar o turno de **{f['nome']}**.")
                continue
            alvos.append(f)
    else:
        alvos = fichas_do_usuario(ctx.guild.id, ctx.author.id)[:1]
        if not alvos:
            await ctx.send("uso: `!turno [personagem1, personagem2, ...]`")
            return
    if not alvos:
        return
    for f in alvos:
        registrar_alvo(ctx, f)
    blocos = [f"**⏭️ {f['nome']}**\n" + "\n".join(processar_turno(f)) for f in alvos]
    await enviar_longo(ctx, "\n\n".join(blocos))


# notas
@bot.command(name="anotar")
async def anotar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades=lambda n: n >= 2)
    if not f:
        return
    if len(resto) < 2:
        await ctx.send("uso: `!anotar [personagem] [nome da nota] [texto]`  (use aspas no nome da nota se tiver espacos)")
        return
    titulo = resto[0][0].strip()
    erro = validar_nome(titulo)
    if erro:
        await ctx.send(erro)
        return
    texto = raw[resto[1][1]:].strip()
    if len(texto) > 3000:
        await ctx.send("o texto da nota pode ter ate 3000 caracteres.")
        return
    if get_nota(f["id"], titulo, exato=True):
        await ctx.send(f"**{f['nome']}** ja tem uma nota **{titulo}**. para mudar o texto use `!editarnota`.")
        return
    db.execute("INSERT INTO notas (ficha_id, titulo, texto) VALUES (?, ?, ?)", (f["id"], titulo, texto))
    db.commit()
    await ctx.send(f"📝 nota **{titulo}** adicionada em **{f['nome']}**.")


@bot.command(name="editarnota")
async def editarnota(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades=lambda n: n >= 2)
    if not f:
        return
    if len(resto) < 2:
        await ctx.send("uso: `!editarnota [personagem] [nota] [novo texto]`")
        return
    nota = get_nota(f["id"], resto[0][0])
    if not nota:
        await ctx.send(msg_nao_tem(f, "nota", resto[0][0]))
        return
    texto = raw[resto[1][1]:].strip()
    if len(texto) > 3000:
        await ctx.send("o texto da nota pode ter ate 3000 caracteres.")
        return
    db.execute("UPDATE notas SET texto=? WHERE id=?", (texto, nota["id"]))
    db.commit()
    await ctx.send(f"📝 nota **{nota['titulo']}** atualizada em **{f['nome']}**.")


@bot.command(name="notas")
async def notas(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), aridades={0, 1})
    if not f:
        return
    if resto:
        n = get_nota(f["id"], resto[0][0])
        if not n:
            await ctx.send(msg_nao_tem(f, "nota", resto[0][0]))
            return
        await enviar_longo(ctx, f"📝 **{n['titulo']}** ({f['nome']})\n{n['texto']}")
        return
    rows = db.execute("SELECT * FROM notas WHERE ficha_id=? ORDER BY titulo", (f["id"],)).fetchall()
    if not rows:
        await ctx.send(f"**{f['nome']}** nao tem notas.")
        return
    blocos = [f"📝 **{r['titulo']}**\n{r['texto']}" for r in rows]
    await enviar_longo(ctx, f"**notas de {f['nome']}**\n\n" + "\n\n".join(blocos))


@bot.command(name="apagar")
async def apagar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!apagar [personagem] [nome da nota]`")
        return
    nota = get_nota(f["id"], resto[0][0], seguro=True)
    if not nota:
        await ctx.send(msg_nao_tem(f, "nota", resto[0][0]))
        return
    db.execute("DELETE FROM notas WHERE id=?", (nota["id"],))
    db.commit()
    await ctx.send(f"🗑️ nota **{nota['titulo']}** apagada de **{f['nome']}**.")


# ordem das fichas e limpezas em massa
def texto_ordem(rows) -> str:
    return "\n".join(f"#{i} **{r['nome']}** (nv {fmt_nivel(r['nivel'])})" for i, r in enumerate(rows, 1))


@bot.command(name="ordem")
async def ordem(ctx, *, raw: str = ""):
    tokens = tokenizar(raw)
    so_mencao = ctx.message.mentions and not re.sub(r"<@!?\d+>", "", raw).strip()
    if not tokens or so_mencao:
        alvo = ctx.message.mentions[0] if ctx.message.mentions else ctx.author
        rows = fichas_do_usuario(ctx.guild.id, alvo.id)
        if not rows:
            await ctx.send("nenhuma ficha encontrada.")
            return
        await ctx.send(f"📋 ordem das fichas de <@{alvo.id}> (comandos sem nome usam a #1):\n" + texto_ordem(rows))
        return
    f, resto = await resolver(ctx, tokens, editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1 or not resto[0][0].isdigit():
        await ctx.send("uso: `!ordem` para ver, ou `!ordem [personagem] [posicao]` para mover. ex.: `!ordem #2 1`")
        return
    lista = fichas_do_usuario(ctx.guild.id, f["dono_id"])
    pos = int(resto[0][0])
    if not 1 <= pos <= len(lista):
        await ctx.send(f"a posicao precisa ser de 1 a {len(lista)}.")
        return
    ids = [r["id"] for r in lista if r["id"] != f["id"]]
    ids.insert(pos - 1, f["id"])
    for i, fid in enumerate(ids, 1):
        db.execute("UPDATE fichas SET ordem=? WHERE id=?", (i, fid))
    db.commit()
    await ctx.send(f"✅ **{f['nome']}** agora e a ficha #{pos}.\n" + texto_ordem(fichas_do_usuario(ctx.guild.id, f["dono_id"])))


@bot.command(name="limpar")
async def limpar(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), editar=True, sem_resto=True, aridades={0})
    if not f:
        return
    qtd = db.execute("SELECT COUNT(*) c FROM itens WHERE ficha_id=?", (f["id"],)).fetchone()["c"]
    if qtd == 0:
        await ctx.send(f"o inventario de **{f['nome']}** ja esta vazio.")
        return
    if not await confirmar(ctx, f"apagar todos os {qtd} item(ns) de **{f['nome']}**?"):
        return
    itens = [{k: r[k] for k in ("nome", "quantidade", "bonus", "equipado", "tipo", "vendaveis")}
             for r in db.execute("SELECT * FROM itens WHERE ficha_id=?", (f["id"],))]
    guardar_lixeira(ctx, "itens", f"{qtd} item(ns) de {f['nome']}",
                    {"ficha_id": f["id"], "ficha_nome": f["nome"], "itens": itens})
    db.execute("DELETE FROM itens WHERE ficha_id=?", (f["id"],))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"🧹 inventario de **{f['nome']}** limpo ({qtd} item(ns) removido(s)).")


@bot.command(name="queimar")
async def queimar(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), editar=True, sem_resto=True, aridades={0})
    if not f:
        return
    qtd = db.execute("SELECT COUNT(*) c FROM notas WHERE ficha_id=?", (f["id"],)).fetchone()["c"]
    if qtd == 0:
        await ctx.send(f"**{f['nome']}** nao tem anotacoes.")
        return
    if not await confirmar(ctx, f"apagar todas as {qtd} anotacao(oes) de **{f['nome']}**?"):
        return
    notas_apagadas = [{k: r[k] for k in ("titulo", "texto")}
                      for r in db.execute("SELECT * FROM notas WHERE ficha_id=?", (f["id"],))]
    guardar_lixeira(ctx, "notas", f"{qtd} anotacao(oes) de {f['nome']}",
                    {"ficha_id": f["id"], "ficha_nome": f["nome"], "notas": notas_apagadas})
    db.execute("DELETE FROM notas WHERE ficha_id=?", (f["id"],))
    db.commit()
    await ctx.send(f"🔥 {qtd} anotacao(oes) de **{f['nome']}** queimada(s).")


@bot.command(name="deletartudo", aliases=["matar"])
async def matar(ctx, *, raw: str = ""):
    alvo = ctx.author
    if ctx.message.mentions:
        alvo = ctx.message.mentions[0]
        if alvo.id != ctx.author.id and not eh_admin(ctx):
            await ctx.send("🚫 so admins podem apagar as fichas de outra pessoa.")
            return
    rows = fichas_do_usuario(ctx.guild.id, alvo.id)
    if not rows:
        await ctx.send("nenhuma ficha para apagar.")
        return
    if any(run_da_ficha(r["id"]) for r in rows):
        await ctx.send("⚔️ alguma dessas fichas esta em uma dungeon; termine ou saia dela antes.")
        return
    nomes = ", ".join(f"**{r['nome']}**" for r in rows)
    de_quem = "suas" if alvo.id == ctx.author.id else f"de <@{alvo.id}>"
    if not await confirmar(ctx, f"apagar todas as {len(rows)} ficha(s) {de_quem} ({nomes}) com itens, status, habilidades e notas?"):
        return
    guardar_lixeira(ctx, "fichas", ", ".join(r["nome"] for r in rows), [serializar_ficha(r) for r in rows])
    db.execute("DELETE FROM fichas WHERE guild_id=? AND dono_id=?", (ctx.guild.id, alvo.id))
    db.commit()
    await ctx.send(f"💀 {len(rows)} ficha(s) apagada(s).")


# habilidades
ATRIBUTOS_E_VITAIS = set(ATRIBUTOS) | {"corpo", "mente"}


def fmt_duracao(d) -> str:
    if d is None:
        return "persistente"
    if d == 1:
        return "vale so o turno atual"
    return f"dura {d} turnos"


def fmt_duracao_hab(bonus: dict, d) -> str:
    """habilidade so de crp/mnt tem efeito imediato; as demais seguem a duracao."""
    if bonus and all(k in ("corpo", "mente") for k in bonus):
        return "efeito imediato ao usar"
    return fmt_duracao(d)


def parse_duracao(txt: str):
    """'persistente' -> (None, True); '3' -> (3, True); invalido -> (None, False)."""
    t = norm(txt)
    if t in ("persistente", "persistentes"):
        return None, True
    if t.isdigit() and int(t) >= 1:
        return int(t), True
    return None, False


@bot.command(name="ch", aliases=["criarhabilidade"])
async def criarhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades=lambda n: 3 <= n <= 5)
    if not f:
        return
    if not 3 <= len(resto) <= 5:
        await ctx.send(
            "uso: `!ch [personagem] [nome] [+sigla,-sigla] [valores] [persistente|turnos] "
            "[perto|longe|magia|jujutsu|melhoria|mel|penalidade|pen]`\n"
            "ex.: `!ch Kage CorteLinear +for,+agi 2,5 perto`\n"
            "ex.: `!ch Kage ArtesMarciais +for 5 persistente mel`\n"
            "ex.: `!ch Kage Furia +for 3 4` (dura 4 turnos)\n"
            "ex.: `!ch Kage Cura +crp 15` (crp e mnt valem na hora em que a habilidade e usada)\n"
            "ex.: `!ch Kage Cura +crp 50%` (porcentagem: 50% do vigor; em atributos, % do valor do atributo)")
        return
    nome = resto[0][0].strip()
    erro = validar_nome(nome)
    if erro:
        await ctx.send(erro)
        return
    bonus, erro = parse_bonus(resto[1][0], resto[2][0], ATRIBUTOS_E_VITAIS)
    if erro:
        await ctx.send(erro)
        return
    duracao, modo = 1, "buff"
    extras = [t for t, _ in resto[3:]]
    for extra in extras:
        if norm(extra) in TIPOS_HABILIDADE:
            modo = TIPOS_HABILIDADE[norm(extra)]
        else:
            duracao, ok = parse_duracao(extra)
            if not ok:
                await ctx.send(f"nao entendi **{extra}**. informe uma duracao e/ou um tipo: "
                               "`perto`, `longe`, `magia`, `jujutsu`, `melhoria` (`mel`) ou "
                               "`penalidade` (`pen`).")
                return
    if get_habilidade(f["id"], nome, exato=True):
        await ctx.send(f"**{f['nome']}** ja tem a habilidade **{nome}**. "
                       "para trocar, apague com `!apagarh` e crie de novo.")
        return
    db.execute("INSERT INTO habilidades (ficha_id, nome, bonus, duracao, modo) VALUES (?, ?, ?, ?, ?)",
               (f["id"], nome, json.dumps(bonus), duracao, modo))
    db.commit()
    if all(k in ("corpo", "mente") for k in bonus):
        dica = "use `!usarh` para aplicar o efeito."
    else:
        dica = "use `!usarh` para ativar, ou `!d20 [sigla] [habilidade]` para somar so naquela rolagem."
    await ctx.send(f"✨ habilidade **{nome}** criada em **{f['nome']}**: {fmt_bonus(bonus)} — "
                   f"{fmt_duracao_hab(bonus, duracao)} • tipo **{nome_tipo_habilidade(modo)}**.\n{dica}")


@bot.command(name="habilidades")
async def habilidades(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), sem_resto=True, aridades={0})
    if not f:
        return
    rows = db.execute("SELECT * FROM habilidades WHERE ficha_id=? ORDER BY nome", (f["id"],)).fetchall()
    if not rows:
        await ctx.send(f"**{f['nome']}** nao tem habilidades. crie com `!ch`.")
        return
    linhas = []
    for r in rows:
        estado = "ativa" if r["ativa"] else "inativa"
        texto = (f"{'🟢' if r['ativa'] else '⚪'} **{r['nome']}** · {estado}\n"
                 f"   Tipo: {nome_tipo_habilidade(r['modo'])} · "
                 f"Efeito: {fmt_bonus(json.loads(r['bonus']))} · "
                 f"Duração: {fmt_duracao_hab(json.loads(r['bonus']), r['duracao'])}")
        if r["ativa"] and r["restam"] is not None:
            texto += f" · Restam {r['restam']} turno(s)"
        linhas.append(texto)
    await enviar_longo(
        ctx,
        f"✨ **Habilidades · {f['nome']}**\n"
        "🟢 ativa · ⚪ inativa\n\n" + "\n\n".join(linhas)
    )


def ativar_habilidade(f, hab) -> list:
    bonus = json.loads(hab["bonus"])
    attrs = {k: v for k, v in bonus.items() if k in ATRIBUTOS}
    vitais = {k: v for k, v in bonus.items() if k in ("corpo", "mente")}
    linhas = []
    if vitais:
        vigor, vontade = maximos(f, get_bonus(f["id"]))
        valores = {"corpo": f["corpo"], "mente": f["mente"]}
        limites = {"corpo": vigor, "mente": vontade}
        for k, v in vitais.items():
            antes = valores[k]
            valores[k] = max(0, min(limites[k], antes + valor_bonus(v, limites[k])))
            linhas.append(f"{SIG[k]}: {antes} → {valores[k]}")
        db.execute("UPDATE fichas SET corpo=?, mente=? WHERE id=?", (valores["corpo"], valores["mente"], f["id"]))
    if attrs:
        if hab["ativa"] and hab["duracao"] is None:
            linhas.append("ja estava ativa (persistente); nada mudou.")
        elif hab["ativa"]:
            db.execute("UPDATE habilidades SET restam=? WHERE id=?", (hab["duracao"], hab["id"]))
            linhas.append(f"renovada: {fmt_bonus(attrs)} — {fmt_duracao(hab['duracao'])}")
        else:
            db.execute("UPDATE habilidades SET ativa=1, restam=? WHERE id=?", (hab["duracao"], hab["id"]))
            linhas.append(f"ativa: {fmt_bonus(attrs)} — {fmt_duracao(hab['duracao'])}")
    db.commit()
    normalizar_vitais(f["id"])
    return linhas


def aplicar_habilidade_alvo(run, hab, alvo_ref):
    bonus = json.loads(hab["bonus"])
    modo = hab["modo"]
    if modo == "buff" and alvo_ref[0] != "j":
        return None, "uma melhoria so pode ter como alvo um aliado."
    if modo == "debuff" and alvo_ref[0] != "m":
        return None, "uma penalidade so pode ter como alvo um inimigo."

    linhas = []
    vitais = {k: v for k, v in bonus.items() if k in ("corpo", "mente")}
    attrs = {k: v for k, v in bonus.items() if k in ATRIBUTOS}
    if alvo_ref[0] == "j":
        alvo = get_ficha_id(alvo_ref[1])
        if not alvo:
            return None, "nao encontrei a ficha-alvo."
        vigor, vontade = maximos(alvo, get_bonus(alvo["id"]))
        valores = {"corpo": alvo["corpo"], "mente": alvo["mente"]}
        limites = {"corpo": vigor, "mente": vontade}
        for k, v in vitais.items():
            antes = valores[k]
            valores[k] = max(0, min(limites[k], antes + valor_bonus(v, limites[k])))
            linhas.append(f"{SIG[k]}: {antes} → {valores[k]}")
        if vitais:
            db.execute("UPDATE fichas SET corpo=?, mente=? WHERE id=?",
                       (valores["corpo"], valores["mente"], alvo["id"]))
        if attrs:
            nome_status = f"habilidade {hab['nome']}"
            db.execute(
                "INSERT INTO status (ficha_id, nome, tipo, bonus, duracao) VALUES (?, ?, 'estatico', ?, ?) "
                "ON CONFLICT(ficha_id, nome) DO UPDATE SET tipo='estatico', bonus=excluded.bonus, duracao=excluded.duracao",
                (alvo["id"], nome_status, json.dumps(attrs), hab["duracao"]),
            )
            linhas.append(f"atributos: {fmt_bonus(attrs)} por {fmt_duracao(hab['duracao'])}")
        db.commit()
        normalizar_vitais(alvo["id"])
        return f"**{alvo['nome']}**", linhas or ["a habilidade nao tem efeitos aplicaveis."]

    monstro = run["inimigos"][alvo_ref[1]]
    for k, v in vitais.items():
        campo = "corpo" if k == "corpo" else "mente"
        maximo = monstro["vigor"] if campo == "corpo" else monstro["vontade"]
        antes = monstro[campo]
        monstro[campo] = max(0, min(maximo, antes + valor_bonus(v, maximo)))
        linhas.append(f"{SIG[k]}: {antes} → {monstro[campo]}")
    if attrs:
        monstro.setdefault("efeitos", []).append({
            "nome": hab["nome"], "bonus": attrs, "duracao": hab["duracao"],
        })
        linhas.append(f"atributos: {fmt_bonus(attrs)} por {fmt_duracao(hab['duracao'])}")
    return f"**{monstro['nome']}**", linhas or ["a habilidade nao tem efeitos aplicaveis."]


@bot.command(name="usarh", aliases=["usarhabilidade"])
async def usarhabilidade(ctx, *, raw: str = ""):
    run = run_do_usuario(ctx.guild.id, ctx.author.id)
    if run and run["estado"] == "combate":
        async with run["lock"]:
            fid = await validar_vez(ctx, run)
            if fid is None:
                return
            f = get_ficha_id(fid)
            registrar_alvo(ctx, f)
            hab_txt, alvo_ref = separar_alvo_dungeon(run, raw)
            nome_hab = hab_txt.strip().strip(ASPAS).strip()
            hab = get_habilidade(fid, nome_hab) if nome_hab else None
            if not hab:
                await ctx.send("uso: `!usarh [habilidade] [alvo opcional]`.")
                return
            if hab["modo"] in ("buff", "debuff"):
                if alvo_ref is None:
                    alvo_ref = ("j", fid) if hab["modo"] == "buff" else None
                if alvo_ref is None:
                    await ctx.send(f"**{hab['nome']}** e uma penalidade; informe um inimigo como alvo.")
                    return
                if alvo_ref[0] == "m" and not inimigo_vivo(run["inimigos"][alvo_ref[1]]):
                    await ctx.send("esse inimigo ja foi derrotado.")
                    return
                if alvo_ref[0] == "j" and run["jogadores"][alvo_ref[1]].get("fugiu"):
                    await ctx.send("esse aliado ja fugiu da batalha e nao pode ser alvo agora.")
                    return
                if hab["modo"] == "buff" and alvo_ref == ("j", fid):
                    linhas = ativar_habilidade(f, hab)
                    if hab["duracao"] is not None and any(k in ATRIBUTOS for k in json.loads(hab["bonus"])):
                        db.execute("UPDATE habilidades SET restam=? WHERE id=?", (hab["duracao"] + 1, hab["id"]))
                        db.commit()
                    await fim_da_acao(ctx, run, fid, [f"✨ **{f['nome']}** usou **{hab['nome']}** em si mesmo.", *linhas])
                    return
                rotulo, resultado = aplicar_habilidade_alvo(run, hab, alvo_ref)
                if rotulo is None:
                    await ctx.send(resultado)
                    return
                if alvo_ref[0] == "j":
                    registrar_alvo(ctx, get_ficha_id(alvo_ref[1]))
                linhas = [f"✨ **{f['nome']}** usou **{hab['nome']}** em {rotulo}."] + resultado
                await fim_da_acao(ctx, run, fid, linhas)
                return
            if hab["modo"] in MODOS_ATAQUE:
                await ctx.send(f"**{hab['nome']}** usa o tipo **{hab['modo']}**; aplique-a com `!atacar [alvo] {hab['modo']} {hab['nome']}`.")
                return
            if alvo_ref:
                await ctx.send(f"**{hab['nome']}** nao e melhoria nem penalidade; "
                               "use `!atacar [alvo] [modo] [habilidade]`.")
                return
            linhas = ativar_habilidade(f, hab)
            await fim_da_acao(ctx, run, fid, [f"✨ **{f['nome']}** usou **{hab['nome']}**.", *linhas])
            return

    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!usarh [personagem] [habilidade]`")
        return
    hab = get_habilidade(f["id"], resto[0][0])
    if not hab:
        await ctx.send(msg_nao_tem(f, "habilidade", resto[0][0]))
        return
    linhas = ativar_habilidade(f, hab)
    await ctx.send(f"✨ **{f['nome']}** usou **{hab['nome']}**.\n" + "\n".join(linhas))


@bot.command(name="desativarh", aliases=["desativarhabilidade"])
async def desativarhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!desativarh [personagem] [habilidade]`")
        return
    hab = get_habilidade(f["id"], resto[0][0])
    if not hab:
        await ctx.send(msg_nao_tem(f, "habilidade", resto[0][0]))
        return
    if not hab["ativa"]:
        await ctx.send(f"**{hab['nome']}** nao esta ativa em **{f['nome']}**.")
        return
    db.execute("UPDATE habilidades SET ativa=0, restam=NULL WHERE id=?", (hab["id"],))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"⏹️ **{hab['nome']}** desativada em **{f['nome']}**.")


@bot.command(name="apagarh", aliases=["apagarhabilidade"])
async def apagarhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!apagarh [personagem] [habilidade]`")
        return
    hab = get_habilidade(f["id"], resto[0][0], seguro=True)
    if not hab:
        await ctx.send(msg_nao_tem(f, "habilidade", resto[0][0]))
        return
    db.execute("DELETE FROM habilidades WHERE id=?", (hab["id"],))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"🗑️ habilidade **{hab['nome']}** apagada de **{f['nome']}**.")


@bot.command(name="renomearh", aliases=["renomearhabilidade"])
async def renomearhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={2})
    if not f:
        return
    if len(resto) != 2:
        await ctx.send("uso: `!renomearh [personagem] [habilidade] [novo nome]`")
        return
    hab = get_habilidade(f["id"], resto[0][0])
    if not hab:
        await ctx.send(msg_nao_tem(f, "habilidade", resto[0][0]))
        return
    novo = resto[1][0].strip()
    erro = validar_nome(novo)
    if erro:
        await ctx.send(erro)
        return
    outra = get_habilidade(f["id"], novo, exato=True)
    if outra and outra["id"] != hab["id"]:
        await ctx.send(f"**{f['nome']}** ja tem a habilidade **{novo}**.")
        return
    db.execute("UPDATE habilidades SET nome=? WHERE id=?", (novo, hab["id"]))
    db.commit()
    await ctx.send(f"✏️ habilidade **{hab['nome']}** agora se chama **{novo}**.")


# restaurar exclusoes e historico
def descrever_lixeira(r) -> str:
    return f"#{r['id']} {r['descricao']} — <@{r['user_id']}> <t:{r['criado_em']}:R>"


@bot.command(name="restaurar")
async def restaurar(ctx, *, raw: str = ""):
    limpar_lixeira()
    if eh_admin(ctx):
        rows = db.execute("SELECT * FROM lixeira WHERE guild_id=? ORDER BY id DESC", (ctx.guild.id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM lixeira WHERE guild_id=? AND user_id=? ORDER BY id DESC",
                          (ctx.guild.id, ctx.author.id)).fetchall()
    if not rows:
        await ctx.send("nada para restaurar. as exclusoes ficam guardadas por 30 minutos.")
        return
    arg = raw.strip().lstrip("#").strip()
    if norm(arg) in ("lista", "ls"):
        await enviar_longo(ctx, "♻️ **da para restaurar** (ate 30 minutos depois de apagar):\n"
                           + "\n".join(descrever_lixeira(r) for r in rows)
                           + "\nuse `!restaurar` (o mais recente) ou `!restaurar [numero]`.")
        return
    if arg:
        escolhida = next((r for r in rows if str(r["id"]) == arg), None)
        if not escolhida:
            await ctx.send("nao achei essa exclusao. veja `!restaurar lista`.")
            return
    else:
        escolhida = rows[0]
    dados = json.loads(escolhida["dados"])
    gid = ctx.guild.id

    if escolhida["tipo"] == "fichas":
        nomes, donos = [], set()
        for d in dados:
            nome = d["nome"]
            if get_ficha(gid, nome, exato=True):
                base, n = nome[:18], 1
                nome = f"{base} (restaurada)"
                while get_ficha(gid, nome, exato=True):
                    n += 1
                    nome = f"{base} (restaurada {n})"
            cols = [k for k in d if k not in ("itens", "status", "notas", "habilidades")]
            valores = [nome if k == "nome" else (999999 if k == "ordem" else d[k]) for k in cols]
            cur = db.execute(f"INSERT INTO fichas ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", valores)
            novo_id = cur.lastrowid
            for r in d["itens"]:
                db.execute("INSERT INTO itens (ficha_id, nome, quantidade, bonus, equipado, tipo, vendaveis) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (novo_id, r["nome"], r["quantidade"], r["bonus"], r["equipado"], r["tipo"],
                            r.get("vendaveis", 0)))
            for r in d["status"]:
                db.execute("INSERT INTO status (ficha_id, nome, tipo, bonus, duracao) VALUES (?, ?, ?, ?, ?)",
                           (novo_id, r["nome"], r["tipo"], r["bonus"], r["duracao"]))
            for r in d["notas"]:
                db.execute("INSERT INTO notas (ficha_id, titulo, texto) VALUES (?, ?, ?)", (novo_id, r["titulo"], r["texto"]))
            for r in d["habilidades"]:
                db.execute("INSERT INTO habilidades (ficha_id, nome, bonus, duracao, ativa, restam, modo, mental, requisitos) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (novo_id, r["nome"], r["bonus"], r["duracao"], r.get("ativa", 0), r.get("restam"),
                            r.get("modo", "buff"), r.get("mental", 0), r.get("requisitos", "{}")))
            nomes.append(nome)
            donos.add(d["dono_id"])
        db.commit()
        for dono in donos:
            renumerar(gid, dono)
        for nome in nomes:
            restaurada = get_ficha(gid, nome, exato=True)
            if restaurada:
                normalizar_vitais(restaurada["id"])
        resumo = f"{len(nomes)} ficha(s): " + ", ".join(f"**{n}**" for n in nomes)
    else:
        f = get_ficha_id(dados["ficha_id"]) or get_ficha(gid, dados["ficha_nome"], exato=True)
        if not f:
            await ctx.send(f"a ficha **{dados['ficha_nome']}** nao existe mais. restaure a ficha primeiro, se ainda der.")
            return
        feitos = pulados = 0
        if escolhida["tipo"] == "itens":
            for r in dados["itens"]:
                if get_item(f["id"], r["nome"], exato=True):
                    pulados += 1
                    continue
                db.execute("INSERT INTO itens (ficha_id, nome, quantidade, bonus, equipado, tipo, vendaveis) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (f["id"], r["nome"], r["quantidade"], r["bonus"], r["equipado"], r["tipo"],
                            r.get("vendaveis", 0)))
                feitos += 1
            rotulo = "item(ns)"
        else:
            for r in dados["notas"]:
                if get_nota(f["id"], r["titulo"], exato=True):
                    pulados += 1
                    continue
                db.execute("INSERT INTO notas (ficha_id, titulo, texto) VALUES (?, ?, ?)", (f["id"], r["titulo"], r["texto"]))
                feitos += 1
            rotulo = "anotacao(oes)"
        db.commit()
        normalizar_vitais(f["id"])
        resumo = f"{feitos} {rotulo} de **{f['nome']}**" + (f" ({pulados} ja existia(m) e foi(ram) mantido(s))" if pulados else "")
    db.execute("DELETE FROM lixeira WHERE id=?", (escolhida["id"],))
    db.commit()
    await ctx.send(f"♻️ restaurado: {resumo}.")


@bot.command(name="historico")
async def historico(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), numero_e_valor=True, aridades={0, 1})
    if not f:
        return
    if not pode_editar(ctx, f):
        await ctx.send("🚫 so o dono da ficha ou um admin ve o historico.")
        return
    qtd = 10
    if resto:
        if len(resto) != 1 or not resto[0][0].isdigit() or not 1 <= int(resto[0][0]) <= 25:
            await ctx.send("uso: `!historico [personagem] [quantidade de 1 a 25]`")
            return
        qtd = int(resto[0][0])
    rows = db.execute("SELECT * FROM historico WHERE ficha_id=? ORDER BY id DESC LIMIT ?", (f["id"], qtd)).fetchall()
    if not rows:
        await ctx.send(f"ainda nao ha alteracoes registradas em **{f['nome']}**.")
        return
    linhas = [f"• <t:{r['criado_em']}:f> <@{r['user_id']}> — `{r['texto'].replace(chr(96), chr(39))}`" for r in rows]
    await enviar_longo(ctx, f"🕘 **historico de {f['nome']}** (ultimas {len(rows)} alteracoes)\n" + "\n".join(linhas))


# xp e niveis
def xp_para_subir(nivel: int) -> int:
    """xp necessario para ir do nivel `nivel` para o proximo."""
    return 100 * nivel


def barra_xp(atual: int, total: int, tamanho: int = 10) -> str:
    cheio = 0 if total <= 0 else max(0, min(tamanho, atual * tamanho // total))
    return "▰" * cheio + "▱" * (tamanho - cheio)


def dar_xp(ficha_id, qtd: int) -> list:
    """soma (ou tira, se negativo) xp e sobe de nivel quantas vezes precisar. fichas de nivel livre nao usam xp.
    retorna as linhas de aviso de subida de nivel."""
    f = get_ficha_id(ficha_id)
    if not f or f["nivel"] == -1:
        return []
    exp, nivel = max(0, f["exp"] + qtd), f["nivel"]
    while nivel < 20 and exp >= xp_para_subir(nivel):
        exp -= xp_para_subir(nivel)
        nivel += 1
    if nivel >= 20:
        exp = 0
    db.execute("UPDATE fichas SET exp=?, nivel=? WHERE id=?", (exp, nivel, ficha_id))
    db.commit()
    if nivel > f["nivel"]:
        return [f"⭐ **{f['nome']}** subiu do nivel {f['nivel']} para o **{nivel}**! "
                f"agora sao {4 * nivel} pontos de atributo (distribua com `!mudar`)."]
    return []


def nivel_efetivo(f) -> int:
    """nivel usado para dimensionar a dungeon (ficha livre: pontos distribuidos / 4)."""
    if f["nivel"] != -1:
        return f["nivel"]
    return max(1, -(-sum(f[k] for k in ATRIBUTOS) // 4))


def registrar_historico(ficha_id, user_id, texto):
    db.execute("INSERT INTO historico (ficha_id, user_id, texto, criado_em) VALUES (?, ?, ?, ?)",
               (ficha_id, user_id, texto[:300], int(time.time())))
    db.execute("DELETE FROM historico WHERE ficha_id=? AND id NOT IN "
               "(SELECT id FROM historico WHERE ficha_id=? ORDER BY id DESC LIMIT 100)", (ficha_id, ficha_id))
    db.commit()


@bot.command(name="xp")
async def ver_xp(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), sem_resto=True, aridades={0})
    if not f:
        return
    if f["nivel"] == -1:
        await ctx.send(f"**{f['nome']}** esta em nivel livre e nao usa xp.")
    elif f["nivel"] >= 20:
        await ctx.send(f"⭐ **{f['nome']}** esta no nivel maximo (20).")
    else:
        precisa = xp_para_subir(f["nivel"])
        await ctx.send(f"⭐ **{f['nome']}** — nivel {f['nivel']} • xp {f['exp']}/{precisa} {barra_xp(f['exp'], precisa)}")


@bot.command(name="upar", aliases=["darxp"])
async def darxp(ctx, *, raw: str = ""):
    if not eh_admin(ctx):
        await ctx.send("🚫 so admins podem dar ou tirar xp.")
        return
    f, resto = await resolver(ctx, tokenizar(raw), numero_e_valor=True, aridades={1})
    if not f:
        return
    if len(resto) != 1 or not re.fullmatch(r"[+-]?\d+", resto[0][0].strip()):
        await ctx.send("uso: `!upar [personagem] [quantidade]` (use `-50` para tirar)")
        return
    if f["nivel"] == -1:
        await ctx.send(f"**{f['nome']}** esta em nivel livre e nao usa xp.")
        return
    registrar_alvo(ctx, f)
    qtd = int(resto[0][0])
    avisos = dar_xp(f["id"], qtd)
    novo = get_ficha_id(f["id"])
    msg = f"✨ **{f['nome']}** {'ganhou' if qtd >= 0 else 'perdeu'} **{abs(qtd)}** xp."
    if novo["nivel"] < 20:
        msg += f" agora: {novo['exp']}/{xp_para_subir(novo['nivel'])} (nivel {novo['nivel']})."
    await ctx.send("\n".join([msg] + avisos))


# dungeons
# - solo ou em grupo (ate 4 jogadores, cada um com uma ficha escolhida)
# - combate por turnos nas regras do sistema (iniciativa por agilidade, acertar/desviar, dano - defesa)
# - corpo em 0 = a ficha e apagada para sempre (nao vai para a lixeira / !restaurar)
# - recompensas ao fim de cada andar: xp, moedas e itens
# - qualquer nivel pode entrar em qualquer dificuldade; a dificuldade define a forca dos inimigos e as recompensas
# - no acampamento podem surgir eventos com pequenas decisoes (baus, armadilhas, altares...)
# - ranking de abates/dano por dungeon, com mvp no fim; o total vai para o ranking do servidor
# - forca e inteligencia causam dano fisico ao corpo
# - o estado das dungeons fica so na memoria: se o bot reiniciar, a dungeon some e as fichas ficam como estao
MAX_JOGADORES = 4
INATIVIDADE_DUNGEON = 2 * 60 * 60   # segundos parada ate ser encerrada
CHANCE_EVENTO = 0.6                 # chance de um evento de decisao aparecer no acampamento

# fator = pontos de atributo do inimigo em relacao aos de um jogador do mesmo nivel (4 por nivel)
# niveis = faixa da dungeon; personagens continuam podendo escolher qualquer dificuldade
DIFICULDADES = {
    "facil":    {"fator": 0.40, "andares": 4, "xp": 0.8, "moedas": 1.0, "niveis": (1, 5),
                 "chance_loot": 0.50, "chance_equip": 0.20, "chance_consumivel_chefe": 0.70},
    "medio":    {"fator": 0.55, "andares": 5, "xp": 1.0, "moedas": 1.6, "niveis": (6, 10),
                 "chance_loot": 0.58, "chance_equip": 0.25, "chance_consumivel_chefe": 0.80},
    "dificil":  {"fator": 0.72, "andares": 6, "xp": 1.4, "moedas": 2.4, "niveis": (11, 15),
                 "chance_loot": 0.66, "chance_equip": 0.30, "chance_consumivel_chefe": 0.90},
    "pesadelo": {"fator": 0.90, "andares": 7, "xp": 2.0, "moedas": 3.5, "niveis": (16, 20),
                 "chance_loot": 0.72, "chance_equip": 0.35, "chance_consumivel_chefe": 0.95},
}
APELIDOS_DIF = {"facil": "facil", "medio": "medio", "media": "medio", "normal": "medio",
                "dificil": "dificil", "pesadelo": "pesadelo"}


def achar_dif(txt: str):
    return APELIDOS_DIF.get(norm(txt))


def dif_do_nivel(nivel: int) -> str:
    for k, d in DIFICULDADES.items():
        lo, hi = d["niveis"]
        if lo <= nivel <= hi:
            return k
    return "pesadelo" if nivel > 20 else "facil"


def indice_dif(run) -> int:
    return list(DIFICULDADES).index(run["dif"])


PERSONALIDADES_MONSTRO = {
    "agressivo": {"nome": "Agressivo", "habilidade": "Investida feroz", "icone": "🔥"},
    "tatico": {"nome": "Tático", "habilidade": "Mira calculada", "icone": "🎯"},
    "curandeiro": {"nome": "Curandeiro", "habilidade": "Ritual de cura", "icone": "💚"},
    "protetor": {"nome": "Protetor", "habilidade": "Postura defensiva", "icone": "🛡️"},
    "covarde": {"nome": "Arisco", "habilidade": "Recuo desesperado", "icone": "🏃"},
}


def _m(nome, pesos, *ataques, personalidade="agressivo"):
    """Modelo de inimigo. Ataques: (nome, atributo de acerto, atributo de dano)."""
    if personalidade not in PERSONALIDADES_MONSTRO:
        raise ValueError(f"personalidade de monstro desconhecida: {personalidade}")
    return {"nome": nome, "pesos": pesos, "ataques": list(ataques), "personalidade": personalidade}


TEMAS = {
    "caverna": {
        "nome": "Caverna dos Goblins",
        "comuns": [
            _m("Goblin", {"forca": 3, "agilidade": 3, "combate": 3, "resistencia": 2, "precisao": 1},
               ("clavada", "combate", "forca"), personalidade="covarde"),
            _m("Morcego Gigante", {"agilidade": 4, "combate": 3, "forca": 2, "resistencia": 1},
               ("mordida", "combate", "forca"), personalidade="agressivo"),
            _m("Goblin Arqueiro", {"precisao": 5, "agilidade": 3, "resistencia": 2, "combate": 1},
               ("flechada", "precisao", "forca"), personalidade="tatico"),
            _m("Xama Goblin", {"inteligencia": 5, "precisao": 3, "sabedoria": 2, "agilidade": 1},
               ("faisca sombria", "precisao", "inteligencia"), personalidade="curandeiro"),
        ],
        "chefe": _m("Rei Goblin", {"forca": 4, "resistencia": 4, "combate": 4, "agilidade": 2, "sabedoria": 2,
                                   "inteligencia": 2, "precisao": 2},
                    ("machadada real", "combate", "forca"), ("grito aterrorizante", "precisao", "inteligencia"),
                    personalidade="agressivo"),
    },
    "cripta": {
        "nome": "Cripta Esquecida",
        "comuns": [
            _m("Esqueleto", {"forca": 3, "combate": 3, "resistencia": 3, "agilidade": 1},
               ("espadada", "combate", "forca"), personalidade="protetor"),
            _m("Zumbi", {"forca": 4, "resistencia": 6, "combate": 2},
               ("pancada podre", "combate", "forca"), personalidade="agressivo"),
            _m("Espectro", {"inteligencia": 5, "precisao": 4, "sabedoria": 3, "agilidade": 2},
               ("toque gelido", "precisao", "inteligencia"), personalidade="curandeiro"),
            _m("Esqueleto Arqueiro", {"precisao": 5, "agilidade": 3, "forca": 2, "resistencia": 2},
               ("flecha ossea", "precisao", "forca"), personalidade="tatico"),
        ],
        "chefe": _m("Lich", {"inteligencia": 6, "sabedoria": 4, "precisao": 5, "resistencia": 2, "combate": 2,
                             "forca": 1},
        ("raio sombrio", "precisao", "inteligencia"), ("mao esqueletica", "combate", "forca"),
        personalidade="curandeiro"),
    },
    "floresta": {
        "nome": "Floresta Sussurrante",
        "comuns": [
            _m("Lobo Selvagem", {"forca": 3, "agilidade": 4, "combate": 4, "resistencia": 2},
               ("mordida feroz", "combate", "forca"), personalidade="agressivo"),
            _m("Aranha Gigante", {"agilidade": 3, "precisao": 4, "forca": 2, "resistencia": 2, "combate": 2},
               ("picada", "precisao", "forca"), personalidade="covarde"),
            _m("Fada Sombria", {"inteligencia": 5, "precisao": 4, "agilidade": 4, "sabedoria": 2},
               ("poeira da loucura", "precisao", "inteligencia"), personalidade="curandeiro"),
            _m("Ent Jovem", {"forca": 4, "resistencia": 7, "combate": 3},
               ("galhada", "combate", "forca"), personalidade="protetor"),
        ],
        "chefe": _m("Urso Corrompido", {"forca": 6, "resistencia": 5, "combate": 5, "agilidade": 3,
                                        "inteligencia": 3},
                    ("garras", "combate", "forca"), ("rugido corrompido", "combate", "inteligencia"),
                    personalidade="agressivo"),
    },
    "ruinas": {
        "nome": "Ruinas Arcanas",
        "comuns": [
            _m("Golem de Pedra", {"forca": 5, "resistencia": 7, "combate": 2},
               ("punho de pedra", "combate", "forca"), personalidade="protetor"),
            _m("Cultista", {"inteligencia": 4, "precisao": 3, "sabedoria": 3, "agilidade": 2},
               ("chama profana", "precisao", "inteligencia"), personalidade="curandeiro"),
            _m("Sentinela Arcana", {"precisao": 5, "inteligencia": 3, "resistencia": 3, "agilidade": 2},
               ("raio de runas", "precisao", "inteligencia"), personalidade="tatico"),
            _m("Escorpiao de Areia", {"forca": 3, "agilidade": 3, "combate": 4, "resistencia": 3},
               ("ferroada", "combate", "forca"), personalidade="covarde"),
        ],
        "chefe": _m("Guardiao Colossal", {"forca": 6, "resistencia": 6, "combate": 4, "inteligencia": 3,
                                          "precisao": 3, "sabedoria": 2},
                    ("esmagar", "combate", "forca"), ("pulso arcano", "precisao", "inteligencia"),
                    personalidade="protetor"),
    },
}

# loot
# equipamentos: (nome, genero, atributos). o bonus depende so da raridade (mesmo nome = mesmo bonus)
EQUIPAMENTOS = [
    ("Espada", "f", ["forca", "combate"]),
    ("Adaga", "f", ["agilidade", "combate"]),
    ("Arco", "m", ["precisao", "agilidade"]),
    ("Cajado", "m", ["inteligencia", "sabedoria"]),
    ("Armadura", "f", ["resistencia"]),
    ("Escudo", "m", ["resistencia", "combate"]),
    ("Amuleto", "m", ["sabedoria", "carisma"]),
    ("Capa", "f", ["agilidade", "sabedoria"]),
]
TIERS = [("de Ferro", "de Ferro"), ("de Aco", "de Aco"), ("Runico", "Runica"), ("Lendario", "Lendaria")]
PESO_TIER = {"facil": [80, 20, 0, 0], "medio": [45, 45, 10, 0], "dificil": [15, 45, 35, 5],
             "pesadelo": [0, 20, 50, 30]}
TIER_MAXIMO_DIF = {"facil": 1, "medio": 2, "dificil": 3, "pesadelo": 3}
CONSUMIVEIS = {
    "Pocao de Cura Menor": {"corpo": 15},
    "Pocao de Cura": {"corpo": "35%"},
    "Pocao de Cura Maior": {"corpo": "60%"},
    "Tonico Mental": {"mente": "35%"},
    "Elixir Revigorante": {"corpo": "30%", "mente": "30%"},
}
PESO_CONSUMIVEL = {
    "facil": {"Pocao de Cura Menor": 70, "Pocao de Cura": 20, "Tonico Mental": 10},
    "medio": {"Pocao de Cura Menor": 35, "Pocao de Cura": 40, "Tonico Mental": 20, "Elixir Revigorante": 5},
    "dificil": {"Pocao de Cura": 40, "Pocao de Cura Maior": 20, "Tonico Mental": 25, "Elixir Revigorante": 15},
    "pesadelo": {"Pocao de Cura": 20, "Pocao de Cura Maior": 40, "Tonico Mental": 15, "Elixir Revigorante": 25},
}


# loja: precos
PRECO_CONSUMIVEL = {"Pocao de Cura Menor": 15, "Pocao de Cura": 40, "Pocao de Cura Maior": 90,
                    "Tonico Mental": 40, "Elixir Revigorante": 80}
PRECO_TIER = [40, 120, 350, 1000]   # ferro, aco, runico, lendario (a loja paga a metade)
NIVEL_TIER = [1, 6, 11, None]       # nivel minimo para comprar; lendario nao e vendido, so achado


def nome_equip(base: str, gen: str, t: int) -> str:
    return f"{base} {TIERS[t][0 if gen == 'm' else 1]}"


def bonus_equip(attrs, t: int) -> dict:
    valor = t + 1
    return {a: (valor if i == 0 else max(1, valor // 2)) for i, a in enumerate(attrs)}


def montar_catalogo() -> dict:
    cat = {}
    for nome, bonus in CONSUMIVEIS.items():
        cat[nome] = {"nome": nome, "tipo": "consumivel", "bonus": bonus, "preco": PRECO_CONSUMIVEL[nome],
                     "nivel": 1, "na_loja": True}
    for base, gen, attrs in EQUIPAMENTOS:
        for t in range(4):
            n = nome_equip(base, gen, t)
            cat[n] = {"nome": n, "tipo": "estatico", "bonus": bonus_equip(attrs, t), "preco": PRECO_TIER[t],
                      "nivel": NIVEL_TIER[t] or 99, "na_loja": NIVEL_TIER[t] is not None}
    return cat


CATALOGO = montar_catalogo()
CATALOGO_NORM = {norm(k): v for k, v in CATALOGO.items()}


def carregar_habilidades_loja() -> list:
    """Valida o JSON: nome, bonus (atributos/vitais), duracao (inteiro ou null), modo e preco."""
    caminho = os.path.join(PASTA, "loja_habilidades.json")
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
    except FileNotFoundError as erro:
        raise RuntimeError(f"arquivo de catalogo nao encontrado: {caminho}") from erro
    except json.JSONDecodeError as erro:
        raise RuntimeError(f"JSON invalido em {caminho}: {erro}") from erro

    if not isinstance(dados, dict) or set(dados) != {"habilidades"} or not isinstance(dados["habilidades"], list):
        raise ValueError(f"{caminho} deve conter somente uma lista chamada 'habilidades'.")

    catalogo, nomes = [], set()
    campos = {"nome", "bonus", "duracao", "modo", "preco"}
    for indice, habilidade in enumerate(dados["habilidades"], 1):
        if not isinstance(habilidade, dict) or set(habilidade) != campos:
            raise ValueError(f"habilidade #{indice} deve conter exatamente: {', '.join(sorted(campos))}.")
        nome, bonus = habilidade["nome"], habilidade["bonus"]
        duracao, modo, preco = habilidade["duracao"], habilidade["modo"], habilidade["preco"]
        if not isinstance(nome, str) or validar_nome(nome.strip()) or not nome.strip():
            raise ValueError(f"nome invalido na habilidade #{indice}.")
        chave_nome = norm(nome)
        if chave_nome in nomes or chave_nome in CATALOGO_NORM:
            raise ValueError(f"nome duplicado ou em conflito com um item da loja: {nome}.")
        if (not isinstance(bonus, dict) or not bonus
                or any(k not in ATRIBUTOS_E_VITAIS for k in bonus)
                or any(type(v) is not int and not (isinstance(v, str) and re.fullmatch(r"[+-]?\d+%", v))
                       for v in bonus.values())):
            raise ValueError(f"bonus invalido na habilidade **{nome}**.")
        if duracao is not None and (type(duracao) is not int or duracao < 1):
            raise ValueError(f"duracao invalida na habilidade **{nome}**; use um inteiro positivo ou null.")
        if not isinstance(modo, str) or norm(modo) not in TIPOS_HABILIDADE:
            raise ValueError(f"tipo invalido na habilidade **{nome}**.")
        modo = TIPOS_HABILIDADE[norm(modo)]
        if type(preco) is not int or preco < 0:
            raise ValueError(f"preco invalido na habilidade **{nome}**; use um inteiro nao negativo.")
        nomes.add(chave_nome)
        catalogo.append({"nome": nome.strip(), "bonus": bonus, "duracao": duracao, "modo": modo, "preco": preco})
    return catalogo


HABILIDADES_LOJA = carregar_habilidades_loja()


def gerar_equip(dif: str, chefe: bool = False, bonus_tier: int = 0) -> dict:
    base, gen, attrs = random.choice(EQUIPAMENTOS)
    t = random.choices(range(4), weights=PESO_TIER[dif])[0]
    t = min(TIER_MAXIMO_DIF[dif], t + (1 if chefe else 0) + bonus_tier)
    return {"nome": nome_equip(base, gen, t), "tipo": "estatico", "bonus": bonus_equip(attrs, t)}


def gerar_consumivel(dif: str) -> dict:
    pesos = PESO_CONSUMIVEL[dif]
    nome = random.choices(list(pesos), weights=list(pesos.values()))[0]
    return {"nome": nome, "tipo": "consumivel", "bonus": CONSUMIVEIS[nome]}


def sortear_loot(dif: str, chefe: bool, rel: float = 1.0) -> list:
    """rel = contribuicao relativa na luta (1.0 = media do grupo). quem contribuiu mais tem mais chance de item
    e uma chance de o equipamento vir uma raridade acima; quem contribuiu menos nunca fica sem chance."""
    regras = DIFICULDADES[dif]
    fator = max(0.6, min(1.5, 0.6 + 0.4 * rel))
    extra = 1 if random.random() < max(0.0, min(0.5, (rel - 1) * 0.5)) else 0
    if chefe:
        itens = [gerar_equip(dif, chefe=True, bonus_tier=extra)]
        if random.random() < min(0.98, regras["chance_consumivel_chefe"] * fator):
            itens.append(gerar_consumivel(dif))
        return itens
    if random.random() < min(0.95, regras["chance_loot"] * fator):
        return [gerar_equip(dif, bonus_tier=extra)
                if random.random() < regras["chance_equip"] else gerar_consumivel(dif)]
    return []


def dar_item(ficha_id, it: dict, qtd: int = 1):
    """item comprado ou achado (dungeon/evento): entra como vendavel."""
    existente = get_item(ficha_id, it["nome"], exato=True)
    if existente:
        db.execute("UPDATE itens SET quantidade=quantidade+?, vendaveis=vendaveis+? WHERE id=?",
                   (qtd, qtd, existente["id"]))
    else:
        db.execute("INSERT INTO itens (ficha_id, nome, quantidade, bonus, tipo, vendaveis) VALUES (?, ?, ?, ?, ?, ?)",
                   (ficha_id, it["nome"], qtd, json.dumps(it["bonus"]), it["tipo"], qtd))
    db.commit()


# inimigos e rolagens
def gerar_monstro(modelo, nivel: int, fator: float, chefe: bool, mult_xp: float) -> dict:
    fator *= min(1.0, 0.55 + 0.15 * nivel)   # niveis baixos pegam inimigos mais fracos (nv 1 = 70%, nv 3+ = 100%)
    pontos = max(2, round(4 * nivel * fator * (1.3 if chefe else 1)))
    soma = sum(modelo["pesos"].values())
    attrs = {k: 0 for k in ATRIBUTOS}
    for k, p in modelo["pesos"].items():
        attrs[k] = min(LIMITE_ATRIBUTO, round(pontos * p / soma))
    corpo = 6 + 2 * nivel + attrs["resistencia"] // 2
    mente = 6 + 2 * nivel + attrs["sabedoria"] // 2
    if chefe:
        corpo, mente = corpo * 3 // 2, mente * 3 // 2
    xp = round((8 + 4 * nivel) * mult_xp * (3 if chefe else 1))
    personalidade = modelo["personalidade"]
    return {"nome": modelo["nome"], "attrs": attrs, "corpo": corpo, "vigor": corpo, "mente": mente,
            "vontade": mente, "ataques": modelo["ataques"], "chefe": chefe, "xp": xp,
            "personalidade": personalidade,
            "habilidade": PERSONALIDADES_MONSTRO[personalidade]["habilidade"],
            "fugiu": False, "efeitos": []}


def rolar_monstro(m: dict, attr: str, extra_bonus: int = 0) -> dict:
    """mesma regra das fichas (d20 + atributo, 20 natural soma 50% do atributo); inimigos nao sofrem penalidades."""
    dado = random.randint(1, 20)
    base = m["attrs"][attr] + sum(
        valor_bonus(efeito["bonus"].get(attr, 0), m["attrs"][attr])
        for efeito in m.get("efeitos", [])
    )
    extra = max(0, int(base * 0.5)) if dado == 20 else 0
    return {"dado": dado, "base": base, "bonus": 0, "habilidade": extra_bonus, "pen": 0, "extra": extra,
            "total": base + dado + extra_bonus + extra}


def rolar_media(c, attrs: tuple[str, str], extra_bonus: int = 0) -> dict:
    """rola um d20 somado a media inteira de dois atributos."""
    if isinstance(c, dict):
        bases = [c["attrs"][attr] for attr in attrs]
        efeitos = c.get("efeitos", [])
        modificadores = [
            sum(valor_bonus(efeito["bonus"].get(attr, 0), c["attrs"][attr]) for efeito in efeitos)
            for attr in attrs
        ]
        base = sum(bases) // 2
        bonus = sum(modificadores) // 2
        penalidade_total = 0
    else:
        bases = [c[attr] for attr in attrs]
        bonus_attrs = get_bonus(c["id"])
        modificadores = [bonus_attrs.get(attr, 0) for attr in attrs]
        base = sum(bases) // 2
        bonus = sum(modificadores) // 2
        penalidades = [
            (penalidade(c["corpo"]) if c["corpo"] <= 40 else 0)
            + (penalidade(c["mente"]) if attr in MENTAIS and c["mente"] <= 40 else 0)
            for attr in attrs
        ]
        penalidade_total = sum(penalidades) // 2
    dado = random.randint(1, 20)
    extra = max(0, int(base * 0.5)) if dado == 20 else 0
    return {
        "dado": dado, "base": base, "bonus": bonus, "habilidade": extra_bonus,
        "pen": penalidade_total, "extra": extra,
        "total": base + bonus + dado + extra_bonus - penalidade_total + extra,
    }


def rolar_atributo_e_metade(c, attrs: tuple[str, tuple[str, int]], extra_bonus: int = 0) -> dict:
    """rola dois atributos somando o primeiro inteiro e a fracao inteira do segundo."""
    principal, (fracionado, divisor) = attrs
    if isinstance(c, dict):
        bases = c["attrs"]
        efeitos = c.get("efeitos", [])
        modificadores = {
            attr: sum(valor_bonus(efeito["bonus"].get(attr, 0), bases[attr]) for efeito in efeitos)
            for attr in (principal, fracionado)
        }
        penalidades = {attr: 0 for attr in (principal, fracionado)}
    else:
        bases = c
        bonus_attrs = get_bonus(c["id"])
        modificadores = {attr: bonus_attrs.get(attr, 0) for attr in (principal, fracionado)}
        penalidades = {
            attr: (penalidade(c["corpo"]) if c["corpo"] <= 40 else 0)
            + (penalidade(c["mente"]) if attr in MENTAIS and c["mente"] <= 40 else 0)
            for attr in (principal, fracionado)
        }
    base = bases[principal] + bases[fracionado] // divisor
    bonus = modificadores[principal] + modificadores[fracionado] // divisor
    penalidade_total = penalidades[principal] + penalidades[fracionado] // divisor
    dado = random.randint(1, 20)
    extra = max(0, int(base * 0.5)) if dado == 20 else 0
    return {
        "dado": dado, "base": base, "bonus": bonus, "habilidade": extra_bonus,
        "pen": penalidade_total, "extra": extra,
        "total": base + bonus + dado + extra_bonus - penalidade_total + extra,
    }


def rolagem(c, attr: str, extra: int = 0) -> dict:
    if isinstance(attr, tuple):
        if isinstance(attr[1], tuple):
            return rolar_atributo_e_metade(c, attr, extra)
        return rolar_media(c, attr, extra)
    return rolar_monstro(c, attr, extra) if isinstance(c, dict) else rolar_attr(c, attr, extra)


def _t(r: dict) -> str:
    return f"{r['total']}" + ("⭐" if r["dado"] == 20 else "")


def golpe(atk, dfn, acerto: str, dano: str, bonus_acerto: int = 0, bonus_dano: int = 0):
    """um golpe completo. atk/dfn: ficha (sqlite Row) ou inimigo (dict). retorna (texto, pontos, campo)."""
    ra, rd = rolagem(atk, acerto, bonus_acerto), rolagem(dfn, acerto)
    cab = f"{SIG[acerto]} {_t(ra)}×{_t(rd)}"
    dif = ra["total"] - rd["total"]
    if dif <= 4:
        return cab + (" → esquivou" if dif < -4 else " → repelido (empate)"), 0, None
    defesa, campo = "resistencia", "corpo"
    da, dd = rolagem(atk, dano, bonus_dano), rolagem(dfn, defesa)
    if isinstance(dano, tuple) and isinstance(dano[1], tuple):
        dano_sig = f"{SIG[dano[0]]}+{SIG[dano[1][0]]}/{dano[1][1]}"
    else:
        dano_sig = "/".join(SIG[attr] for attr in dano) if isinstance(dano, tuple) else SIG[dano]
    cab += f" ✔ • dano {dano_sig} {_t(da)}×{SIG[defesa]} {_t(dd)}"
    d = da["total"] - dd["total"]
    if d <= 4:
        return cab + " → defendido", 0, None
    return cab + f" → 💥 {d} de {campo}", d, campo


def aplicar_dano(alvo, pontos: int, campo: str):
    if isinstance(alvo, dict):
        antes = alvo[campo]
        alvo[campo] = max(0, antes - pontos)
        return antes, alvo[campo]
    f = get_ficha_id(alvo["id"])
    antes = f[campo]
    novo = max(0, antes - pontos)
    db.execute(f"UPDATE fichas SET {campo}=? WHERE id=?", (novo, alvo["id"]))
    db.commit()
    return antes, novo


def inimigo_vivo(m: dict) -> bool:
    return m["corpo"] > 0 and not m.get("fugiu", False)


def processar_efeitos_monstro(m: dict) -> list:
    linhas = []
    efeitos = []
    for efeito in m.get("efeitos", []):
        if efeito["duracao"] is None:
            efeitos.append(efeito)
            continue
        efeito["duracao"] -= 1
        if efeito["duracao"] > 0:
            efeitos.append(efeito)
        else:
            linhas.append(f"⌛ efeito **{efeito['nome']}** acabou em **{m['nome']}**.")
    m["efeitos"] = efeitos
    return linhas


def acao_ataque(f, m: dict, acerto: str, dano: str, nome_hab=None, bonus_acerto=0, bonus_dano=0,
                reg=None, tot=None) -> list:
    """reg: contagem do andar do jogador; tot: contagem da dungeon inteira (ranking).
    ambos tem "dano", "acertos" e "abates"; dano conta so o que realmente tirou do inimigo."""
    contas = [c for c in (reg, tot) if c is not None]
    vivo_antes = inimigo_vivo(m)
    texto, pts, campo = golpe(f, m, acerto, dano, bonus_acerto, bonus_dano)
    extra = f" com {nome_hab}" if nome_hab else ""
    linha = f"⚔️ **{f['nome']}** → **{m['nome']}**{extra}: {texto}"
    if "✔" in texto:
        for c in contas:
            c["acertos"] = c.get("acertos", 0) + 1
    if pts:
        antes, depois = aplicar_dano(m, pts, campo)
        linha += f" ({campo} {antes}→{depois})"
        for c in contas:
            c["dano"] = c.get("dano", 0) + antes - depois
    linhas = [linha]
    if not inimigo_vivo(m):
        linhas.append(f"☠️ **{m['nome']}** foi derrotado por **{f['nome']}**!")
        if vivo_antes:
            for c in contas:
                c["abates"] = c.get("abates", 0) + 1
    return linhas


def acao_monstro(run: dict, m: dict) -> list:
    alvos = [f for f in (get_ficha_id(fid) for fid in ativos(run)) if f and f["corpo"] > 0]
    if not alvos:
        return []

    personalidade = m["personalidade"]
    habilidade = m["habilidade"]
    if personalidade == "curandeiro":
        aliados = [monstro for monstro in run["inimigos"] if inimigo_vivo(monstro)]
        feridos = [aliado for aliado in aliados if aliado["corpo"] <= aliado["vigor"] * 0.65]
        if feridos and random.random() < 0.65:
            alvo = min(feridos, key=lambda aliado: aliado["corpo"] / aliado["vigor"])
            antes = alvo["corpo"]
            alvo["corpo"] = min(alvo["vigor"], antes + max(3, alvo["vigor"] // 4))
            return [f"💚 **{m['nome']}** usou **{habilidade}** em **{alvo['nome']}**: "
                    f"corpo {antes}→{alvo['corpo']}."]

    if personalidade == "protetor" and not any(
        efeito["nome"] == habilidade for efeito in m.get("efeitos", [])
    ) and random.random() < 0.3:
        m.setdefault("efeitos", []).append({
            "nome": habilidade, "bonus": {"resistencia": 3}, "duracao": 2,
        })
        return [f"🛡️ **{m['nome']}** usou **{habilidade}**: resistência +3 até o próximo turno."]

    if personalidade == "covarde" and not m["chefe"] and m["corpo"] <= m["vigor"] // 4 \
            and random.random() < 0.5:
        m["fugiu"] = True
        return [f"🏃 **{m['nome']}** usou **{habilidade}** e fugiu do combate; não concede XP próprio."]

    if personalidade == "tatico":
        f = min(alvos, key=lambda ficha: ficha["corpo"] / maximos(ficha, get_bonus(ficha["id"]))[0])
    else:
        f = random.choice(alvos)
    nome_atk, acerto, dano = random.choice(m["ataques"])
    bonus = 0
    especial = random.random() < (0.3 if personalidade == "agressivo" else 0.4 if personalidade == "tatico" else 0)
    if especial:
        nome_atk = habilidade
        bonus = 3 if personalidade == "tatico" else 2
    texto, pts, campo = golpe(m, f, acerto, dano, bonus, bonus)
    linha = f"👹 **{m['nome']}** → **{f['nome']}** ({nome_atk}): {texto}"
    if pts:
        antes, depois = aplicar_dano(f, pts, campo)
        linha += f" ({campo} {antes}→{depois})"
    return [linha]


# estado das dungeons
DUNGEONS = {}   # id do canal -> dungeon


class EmCombate(commands.CheckFailure):
    pass


# comandos que nao valem para quem esta no meio de um combate (as acoes certas sao !atacar, !usar...)
BLOQUEADOS_EM_COMBATE = {
    "mudar", "nivel", "equipar", "desequipar", "usaritem", "desativarh", "turno",
    "dano", "descartar", "limpar", "addstatus", "rmstatus", "apagarh", "deletartudo", "deletar",
    "mudardono", "upar", "transferir", "pix", "comprar", "vender", "darmoedas",
}


def run_da_ficha(ficha_id):
    for r in DUNGEONS.values():
        if ficha_id in r["jogadores"]:
            return r
    return None


def run_do_usuario(guild_id, user_id):
    for r in DUNGEONS.values():
        if r["guild"] == guild_id and any(p["user"] == user_id for p in r["jogadores"].values()):
            return r
    return None


def run_em_combate(guild_id, user_id):
    """so vale para quem ainda esta lutando (quem fugiu da batalha fica livre)."""
    for r in DUNGEONS.values():
        if r["guild"] == guild_id and r["estado"] == "combate" \
                and any(p["user"] == user_id and not p.get("fugiu") for p in r["jogadores"].values()):
            return r
    return None


def ativos(run) -> list:
    """jogadores que ainda estao na batalha atual (nao fugiram)."""
    return [fid for fid, p in run["jogadores"].items() if not p.get("fugiu")]


@bot.check
async def sem_atalhos_em_combate(ctx):
    if ctx.guild and ctx.command and ctx.command.name in BLOQUEADOS_EM_COMBATE \
            and run_em_combate(ctx.guild.id, ctx.author.id):
        raise EmCombate()
    return True


def jogador_do_usuario(run, user_id):
    return next((fid for fid, p in run["jogadores"].items() if p["user"] == user_id), None)


def separar_alvo_dungeon(run, texto: str, inimigos: bool = True):
    """separa o sufixo que identifica um aliado ou inimigo do texto do comando."""
    if not run:
        return texto, None
    tokens = tokenizar(texto)
    if len(tokens) < 2:
        return texto, None
    candidatos = []
    for fid in run["jogadores"]:
        ficha = get_ficha_id(fid)
        if ficha:
            candidatos.append((ficha["nome"], ("j", fid)))
    if inimigos:
        candidatos.extend((monstro["nome"], ("m", i)) for i, monstro in enumerate(run["inimigos"]))
    encontrados = []
    for i, (_, inicio) in enumerate(tokens):
        prefixo = texto[:inicio].strip()
        if not prefixo:
            continue
        sufixo = texto[inicio:].strip().strip(ASPAS).strip()
        numero = re.fullmatch(r"#?(\d+)", sufixo)
        if inimigos and numero:
            idx = int(numero.group(1)) - 1
            if 0 <= idx < len(run["inimigos"]):
                encontrados.append((len(sufixo), prefixo, ("m", idx)))
        for nome, ref in candidatos:
            if norm(sufixo) == norm(nome):
                encontrados.append((len(nome), prefixo, ref))
    if not encontrados:
        return texto, None
    _, prefixo, ref = max(encontrados, key=lambda candidato: candidato[0])
    return prefixo, ref


def mult_dif(run) -> dict:
    return DIFICULDADES[run["dif"]]


def novo_jogador(run, f, user_id):
    run["jogadores"][f["id"]] = {"user": user_id, "descansou": False, "fugiu": False}
    run["resumo"][f["id"]] = {"nome": f["nome"], "user": user_id, "xp": 0, "moedas": 0, "itens": [], "fim": None,
                              "abates": 0, "dano": 0, "acertos": 0, "eventos": 0}


def texto_painel(run) -> str:
    linhas = []
    for i, m in enumerate(run["inimigos"], 1):
        if inimigo_vivo(m):
            perfil = PERSONALIDADES_MONSTRO[m["personalidade"]]
            linhas.append(f"{i}. {'👑 ' if m['chefe'] else ''}**{m['nome']}** — corpo {m['corpo']}/{m['vigor']} • "
                          f"mente {m['mente']}/{m['vontade']} • {perfil['icone']} {perfil['nome']} "
                          f"({m['habilidade']})")
        elif m.get("fugiu"):
            linhas.append(f"{i}. ~~{m['nome']}~~ 🏃 fugiu")
        else:
            linhas.append(f"{i}. ~~{m['nome']}~~ ☠️")
    linhas.append("")
    for fid in run["jogadores"]:
        f = get_ficha_id(fid)
        if f:
            vigor, vontade = maximos(f, {})
            icone = "🏃" if run["jogadores"][fid].get("fugiu") else "🛡️"
            linhas.append(f"{icone} **{f['nome']}** — corpo {f['corpo']}/{vigor} • mente {f['mente']}/{vontade}"
                          + (" (fugiu da luta)" if icone == "🏃" else ""))
    return "\n".join(linhas)


def texto_vez(run) -> str:
    tipo, ref = run["ordem"][run["pos"]]
    if tipo == "m":
        return f"vez de {run['inimigos'][ref]['nome']}."
    f = get_ficha_id(ref)
    return f"vez de **{f['nome']}**." if f else ""


def avancar_pos(run):
    run["pos"] += 1
    if run["pos"] >= len(run["ordem"]):
        run["pos"] = 0
        run["rodada"] += 1


def verificar_mortes(run) -> list:
    """corpo em 0 = a ficha e apagada para sempre."""
    linhas = []
    for fid in list(run["jogadores"]):
        f = get_ficha_id(fid)
        if f is None:
            del run["jogadores"][fid]
            continue
        if f["corpo"] <= 0:
            db.execute("DELETE FROM fichas WHERE id=?", (fid,))
            db.commit()
            renumerar(run["guild"], f["dono_id"])
            del run["jogadores"][fid]
            run["resumo"][fid]["fim"] = "morreu"
            linhas.append(f"💀 **{f['nome']}** morreu na dungeon. a ficha foi apagada para sempre.")
    return linhas


def gerar_inimigos(run) -> list:
    d, tema = mult_dif(run), TEMAS[run["tema"]]
    andar, n, nivel = run["andar"], len(run["jogadores"]), run["nivel"]
    fator = d["fator"] * (1 + 0.05 * (andar - 1))
    inimigos = []
    if andar >= d["andares"]:
        inimigos.append(gerar_monstro(tema["chefe"], nivel, fator, True, d["xp"]))
        qtd = n - 1
    else:
        qtd = min(5, n + (random.choice([0, 0, 1]) if andar >= 2 else 0))
    # decisoes dos eventos: barulho atrai mais inimigos, atalhos evitam alguns
    ajuste = run.pop("extra_inimigo", 0) - run.pop("menos_inimigo", 0)
    qtd = max(0 if andar >= d["andares"] else 1, min(6, qtd + ajuste))
    for _ in range(qtd):
        inimigos.append(gerar_monstro(random.choice(tema["comuns"]), nivel, fator, False, d["xp"]))
    contagem = {}
    for m in inimigos:
        contagem[m["nome"]] = contagem.get(m["nome"], 0) + 1
    usados = {}
    for m in inimigos:
        if contagem[m["nome"]] > 1:
            usados[m["nome"]] = usados.get(m["nome"], 0) + 1
            m["nome"] += " " + chr(64 + usados[m["nome"]])
    return inimigos


async def iniciar_andar(ctx, run):
    if not run.pop("repetir", False):   # depois de uma fuga geral o mesmo andar e refeito
        run["andar"] += 1
    run["contrib"] = {}
    for p in run["jogadores"].values():
        p["fugiu"] = False
    niveis = [nivel_efetivo(f) for f in (get_ficha_id(fid) for fid in run["jogadores"]) if f]
    lo, hi = mult_dif(run)["niveis"]
    run["nivel"] = max(lo, min(hi, round(sum(niveis) / len(niveis))))   # inimigos ficam na faixa da dungeon
    run["inimigos"] = gerar_inimigos(run)
    run["estado"], run["rodada"] = "combate", 1
    for p in run["jogadores"].values():
        p["descansou"] = False
    itens = []
    for fid in run["jogadores"]:
        f = get_ficha_id(fid)
        itens.append((rolar_attr(f, "agilidade")["total"], random.random(), ("j", fid), f["nome"]))
    for i, m in enumerate(run["inimigos"]):
        itens.append((rolar_monstro(m, "agilidade")["total"], random.random(), ("m", i), m["nome"]))
    itens.sort(key=lambda x: (-x[0], -x[1]))
    run["ordem"], run["pos"] = [x[2] for x in itens], 0
    total = mult_dif(run)["andares"]
    chefe = "  👑 andar do chefe!" if run["andar"] >= total else ""
    linhas = [f"🏰 **{TEMAS[run['tema']]['nome']}** ({run['dif']}) — andar {run['andar']}/{total}{chefe}",
              "🎯 iniciativa: " + " > ".join(f"{x[3]} ({x[0]})" for x in itens)]
    await processar_ordem(ctx, run, linhas)


async def processar_ordem(ctx, run, buf=None):
    """roda os turnos dos inimigos ate chegar a vez de um jogador (ou ate o fim do andar)."""
    buf = buf if buf is not None else []
    for _ in range(500):
        if not run["jogadores"]:
            await fim_dungeon(ctx, run, "derrota", buf)
            return
        if not any(inimigo_vivo(m) for m in run["inimigos"]):
            await vitoria_andar(ctx, run, buf)
            return
        if not ativos(run):   # todos fugiram da batalha: voltam ao acampamento, a dungeon continua
            run["estado"], run["repetir"] = "pausa", True
            for p in run["jogadores"].values():
                p["fugiu"], p["descansou"] = False, False
            buf.append("🏃 todos fugiram da batalha! o grupo volta ao acampamento e continua na dungeon.\n"
                       "descansem (`!descansar`, `!usaritem`...) e o lider usa `!dungeon avancar` para refazer este andar "
                       "(com inimigos novos). `!dungeon sair` deixa a dungeon.")
            await enviar_longo(ctx, "\n".join(buf))
            return
        tipo, ref = run["ordem"][run["pos"]]
        if tipo == "m":
            m = run["inimigos"][ref]
            if inimigo_vivo(m):
                buf += acao_monstro(run, m)
                buf += processar_efeitos_monstro(m)
                buf += verificar_mortes(run)
            avancar_pos(run)
            continue
        if ref not in run["jogadores"] or run["jogadores"][ref].get("fugiu"):
            avancar_pos(run)
            continue
        f = get_ficha_id(ref)
        buf.append(f"➡️ **rodada {run['rodada']}** — vez de **{f['nome']}** (<@{run['jogadores'][ref]['user']}>)\n"
                   + texto_painel(run)
                   + "\n`!atacar [alvo] [perto|longe|magia|jujutsu] [habilidade]` • "
                     "`!usar [item] [aliado opcional]` • `!usarh [habilidade] [alvo opcional]` • "
                     "`!descansar [crp|mnt]` • `!fugir` • `!passar`")
        await enviar_longo(ctx, "\n".join(buf))
        return
    await enviar_longo(ctx, "\n".join(buf))


async def fim_da_acao(ctx, run, fid, linhas):
    """termina o turno do jogador: status/duracoes, mortes e passa para o proximo."""
    f = get_ficha_id(fid)
    if f:
        efeitos = [l for l in processar_turno(f) if l != "sem efeitos neste turno."]
        if efeitos:
            linhas.append(f"⏭️ **{f['nome']}** — fim do turno: " + " • ".join(efeitos))
    linhas += verificar_mortes(run)
    avancar_pos(run)
    await processar_ordem(ctx, run, linhas)


async def vitoria_andar(ctx, run, buf):
    d = mult_dif(run)
    total, andar = d["andares"], run["andar"]
    ultimo = andar >= total
    xp = sum(m["xp"] for m in run["inimigos"] if not m.get("fugiu"))
    if ultimo:
        xp += round((40 + 10 * run["nivel"]) * d["xp"])
    base_moedas = (8 + 2 * run["nivel"]) * d["moedas"] * (3 if ultimo else 1)
    buf.append(f"🏁 **andar {andar}/{total} limpo!**" if not ultimo else "🏆 **o chefe caiu! dungeon concluida!**")
    # recompensa por contribuicao: pontos = dano real + 2 por acerto. media do grupo = 1.0; o minimo e 60% do xp base
    # e o maximo 160%, mais uma variacao aleatoria de +-15% para cada um
    lutando = ativos(run)
    pont = {fid: run["contrib"].get(fid, {"dano": 0, "acertos": 0, "abates": 0}) for fid in lutando}
    pont = {fid: c["dano"] + 2 * c["acertos"] + 5 * c.get("abates", 0) for fid, c in pont.items()}
    soma, melhor = sum(pont.values()), max(pont.values(), default=0)
    for fid, p in list(run["jogadores"].items()):
        f = get_ficha_id(fid)
        if not f:
            continue
        if p.get("fugiu"):
            buf.append(f"🏃 **{f['nome']}** fugiu da luta: sem recompensa neste andar.")
            continue
        c = run["contrib"].get(fid, {"dano": 0, "acertos": 0, "abates": 0})
        rel = pont[fid] / soma * len(lutando) if soma > 0 else 1.0
        mult = max(0.6, min(1.6, 0.6 + 0.4 * rel)) * random.uniform(0.85, 1.15)
        xp_p = max(1, round(xp * mult))
        moedas_p = max(1, round(base_moedas * mult))
        db.execute("UPDATE fichas SET moedas=moedas+? WHERE id=?", (moedas_p, fid))
        db.commit()
        res = run["resumo"][fid]
        res["moedas"] += moedas_p
        itens = sortear_loot(run["dif"], ultimo, rel)
        for it in itens:
            dar_item(fid, it)
            res["itens"].append(it["nome"])
        partes = []
        destaque = " 🏅" if len(lutando) > 1 and soma > 0 and pont[fid] == melhor else ""
        luta = f"(abates {c.get('abates', 0)}, dano {c['dano']}, acertos {c['acertos']})"
        if f["nivel"] != -1:
            res["xp"] += xp_p
            partes.append(f"+{xp_p} xp • +{moedas_p} 💰 {luta}{destaque}")
        else:
            partes.append(f"(nivel livre: sem xp) +{moedas_p} 💰 {luta}{destaque}")
        if itens:
            partes.append("itens: " + ", ".join(
                f"**{it['nome']}** [{fmt_bonus(it['bonus'])}]" for it in itens))
        buf.append(f"🎁 **{f['nome']}**: " + " • ".join(partes))
        buf += dar_xp(fid, xp_p)
        registrar_historico(fid, p["user"], f"dungeon {run['tema']} andar {andar}: +{xp_p} xp, +{moedas_p} moedas"
                            + (", " + ", ".join(it["nome"] for it in itens) if itens else ""))
    for p in run["jogadores"].values():
        p["fugiu"] = False
    if ultimo:
        await fim_dungeon(ctx, run, "vitoria", buf)
        return
    run["estado"] = "pausa"
    if len(run["resumo"]) > 1:
        buf.append("📊 parcial: " + " • ".join(f"{r['nome']} {r['abates']}☠️" for _, r in ordem_ranking(run)))
    buf.append("🏕️ **acampamento** — recuperem o folego: `!descansar [crp|mnt]` (uma vez cada, recupera 20%), "
               "`!usaritem`, `!equipar`, `!transferir` (entre o grupo), etc.\n"
               "o lider usa `!dungeon avancar` para descer ao proximo andar; `!dungeon sair` sai com o que ja ganhou.")
    if random.random() < CHANCE_EVENTO:
        texto = criar_evento(run)
        if texto:
            buf.append(texto)
    await enviar_longo(ctx, "\n".join(buf))


async def fim_dungeon(ctx, run, motivo: str, buf=None):
    DUNGEONS.pop(run["canal"], None)
    buf = buf if buf is not None else []
    titulo = {"vitoria": "🏆 **dungeon concluida!**", "derrota": "☠️ **o grupo foi derrotado.**",
              "saiu": "🚪 **o grupo deixou a dungeon.**", "cancelada": "❌ **dungeon cancelada.**"}[motivo]
    if motivo != "vitoria":
        buf.append(titulo)
    mvp = None
    if motivo == "vitoria":
        mvp = escolher_mvp(run)
        if mvp is not None:
            d = mult_dif(run)
            premio = round((20 + 5 * run["nivel"]) * d["moedas"])
            db.execute("UPDATE fichas SET moedas=moedas+? WHERE id=?", (premio, mvp))
            db.commit()
            run["resumo"][mvp]["moedas"] += premio
            buf.append(f"👑 **MVP: {run['resumo'][mvp]['nome']}** — {run['resumo'][mvp]['abates']} abate(s)! "
                       f"premio: +{premio} 💰")
    linhas = []
    for fid, res in run["resumo"].items():
        fim = res["fim"] or ("concluiu" if motivo == "vitoria" else "saiu com vida")
        if fim == "morreu":
            linhas.append(f"💀 **{res['nome']}** — morreu (ficha apagada) • {res['abates']} abate(s)")
            continue
        itens = ", ".join(res["itens"]) if res["itens"] else "nenhum item"
        linhas.append(f"• **{res['nome']}** — {fim} • +{res['xp']} xp • +{res['moedas']} 💰 • {itens}")
    if linhas and motivo != "cancelada":
        buf.append("📜 **resumo**\n" + "\n".join(linhas))
        buf.append(texto_ranking(run, mvp))
        for fid, res in run["resumo"].items():
            venceu = motivo == "vitoria" and fid in run["jogadores"]
            atualizar_ranking(run["guild"], res["user"], res["abates"], res["dano"], fid == mvp, venceu)
    if buf:
        await enviar_longo(ctx, "\n".join(buf))


# ranking
MEDALHAS = ["🥇", "🥈", "🥉"]


def ordem_ranking(run) -> list:
    """quem mais matou primeiro; desempate por dano e depois acertos."""
    return sorted(run["resumo"].items(),
                  key=lambda kv: (-kv[1]["abates"], -kv[1]["dano"], -kv[1]["acertos"], norm(kv[1]["nome"])))


def escolher_mvp(run):
    """mvp = quem mais matou entre quem chegou ao fim (precisa de grupo com 2+ pessoas e pelo menos 1 abate)."""
    if len(run["resumo"]) < 2:
        return None
    vivos = [(fid, r) for fid, r in ordem_ranking(run) if fid in run["jogadores"]]
    if not vivos or vivos[0][1]["abates"] == 0:
        return None
    return vivos[0][0]


def texto_ranking(run, mvp=None) -> str:
    linhas = []
    for i, (fid, r) in enumerate(ordem_ranking(run)):
        icone = MEDALHAS[i] if i < 3 else f"{i + 1}."
        morto = " 💀" if r["fim"] == "morreu" else (" 🚪" if r["fim"] == "saiu com vida" else "")
        coroa = " 👑 **MVP**" if fid == mvp else ""
        linhas.append(f"{icone} **{r['nome']}**{morto} — {r['abates']} abate(s) • {r['dano']} de dano • "
                      f"{r['acertos']} acerto(s){coroa}")
    return "🏆 **ranking da dungeon**\n" + "\n".join(linhas)


def atualizar_ranking(guild_id, user_id, abates, dano, mvp, vitoria):
    db.execute("INSERT INTO ranking (guild_id, user_id, abates, dano, mvps, dungeons, vitorias) "
               "VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
               "abates=abates+excluded.abates, dano=dano+excluded.dano, mvps=mvps+excluded.mvps, "
               "dungeons=dungeons+1, vitorias=vitorias+excluded.vitorias",
               (guild_id, user_id, abates, dano, int(bool(mvp)), int(bool(vitoria))))
    db.commit()


@tasks.loop(minutes=10)
async def limpar_dungeons():
    agora = time.time()
    for cid, run in list(DUNGEONS.items()):
        if agora - run["ultima"] > INATIVIDADE_DUNGEON:
            DUNGEONS.pop(cid, None)
            canal = bot.get_channel(cid)
            if canal:
                try:
                    await canal.send("⌛ dungeon encerrada por inatividade. as fichas que estavam nela foram mantidas.")
                except discord.HTTPException as erro:
                    print(f"nao foi possivel avisar sobre a dungeon inativa no canal {cid}: {erro}")


# eventos de decisao (acampamento)
# aparecem depois de um andar limpo; eventos coletivos recebem votos do grupo e eventos solo sao decididos pelo explorador.
# armadilhas machucam mas nunca matam (o corpo para em 1).
class EventoInvalido(Exception):
    """a opcao nao pode ser feita agora (ex.: sem moedas); o evento continua aberto."""


def cd_evento(run) -> int:
    return 10 + round(0.6 * run["nivel"])


def teste_evento(run, f, attr):
    r = rolar_attr(f, attr)
    cd = cd_evento(run)
    ok = r["total"] >= cd
    return ok, f"{SIG[attr]} {_t(r)} vs cd {cd} {'✔' if ok else '✘'}"


def ev_moedas(run, fid, mult=1.0) -> str:
    qtd = max(1, round((10 + 3 * run["nivel"]) * mult_dif(run)["moedas"] * mult * random.uniform(0.7, 1.3)))
    db.execute("UPDATE fichas SET moedas=moedas+? WHERE id=?", (qtd, fid))
    db.commit()
    run["resumo"][fid]["moedas"] += qtd
    return f"+{qtd} 💰"


def ev_item(run, fid, chance_equip=0.0, it=None) -> str:
    chance_equip = max(chance_equip, DIFICULDADES[run["dif"]]["chance_equip"])
    it = it or (gerar_equip(run["dif"]) if random.random() < chance_equip else gerar_consumivel(run["dif"]))
    dar_item(fid, it)
    run["resumo"][fid]["itens"].append(it["nome"])
    return f"**{it['nome']}** [{fmt_bonus(it['bonus'])}]"


def ev_dano(run, fid, campo="corpo", mult=1.0) -> str:
    f = get_ficha_id(fid)
    pts = max(1, round((3 + run["nivel"] // 2 + random.randint(0, 4)) * (1 + 0.25 * indice_dif(run)) * mult))
    novo = max(1 if campo == "corpo" else 0, f[campo] - pts)
    db.execute(f"UPDATE fichas SET {campo}=? WHERE id=?", (novo, fid))
    db.commit()
    return f"**{f['nome']}** {SIG[campo]} {f[campo]}→{novo}"


def ev_cura(fid, campo, pct) -> str:
    f = get_ficha_id(fid)
    vigor, vontade = maximos(f, {})
    maximo = vigor if campo == "corpo" else vontade
    novo = min(maximo, f[campo] + max(1, maximo * pct // 100))
    db.execute(f"UPDATE fichas SET {campo}=? WHERE id=?", (novo, fid))
    db.commit()
    return f"**{f['nome']}** {SIG[campo]} {f[campo]}→{novo}"


def ev_status(fid, nome, bonus, duracao) -> str:
    db.execute("INSERT INTO status (ficha_id, nome, tipo, bonus, duracao) VALUES (?, ?, 'estatico', ?, ?) "
               "ON CONFLICT(ficha_id, nome) DO UPDATE SET tipo='estatico', bonus=excluded.bonus, "
               "duracao=excluded.duracao", (fid, nome, json.dumps(bonus), duracao))
    db.commit()
    return f"status **{nome}** ({fmt_bonus(bonus)}, {duracao} turnos)"


def ev_pagar(fid, preco):
    f = get_ficha_id(fid)
    if f["moedas"] < preco:
        raise EventoInvalido(f"💰 **{f['nome']}** tem so {f['moedas']} moeda(s) e precisa de {preco}. escolha outra opcao.")
    db.execute("UPDATE fichas SET moedas=moedas-? WHERE id=?", (preco, fid))
    db.commit()


def _op(chave, rotulo, efeito):
    return {"chave": chave, "rotulo": rotulo, "efeito": efeito}


def ignorar(_run, _fid):
    return ["🚶 o grupo deixa isso para tras e segue em frente."]


def evento_bau(run):
    def abrir(run, fid):
        sorte = random.random()
        if sorte < 0.2:
            return [f"🪤 era um bau armadilhado! agulhas saltam da fechadura: {ev_dano(run, fid)}"]
        if sorte < 0.3:
            return [f"👅 o bau era um **mimico**! ele morde e foge, deixando cair algumas moedas: "
                    f"{ev_dano(run, fid, mult=1.4)} • {ev_moedas(run, fid, 0.5)}"]
        return [f"✨ o bau abre sem problemas: {ev_moedas(run, fid)} • {ev_item(run, fid, 0.25)}"]

    def examinar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "sabedoria")
        if ok:
            return [f"🔍 {t} — **{f['nome']}** acha e desarma o mecanismo. dentro: "
                    f"{ev_moedas(run, fid, 1.2)} • {ev_item(run, fid, 0.35)}"]
        return [f"🔍 {t} — nao percebe nada de estranho e abre mesmo assim..."] + abrir(run, fid)

    def arrombar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "forca")
        if ok:
            return [f"🪓 {t} — a tampa voa longe: {ev_moedas(run, fid)} • {ev_item(run, fid, 0.5)}"]
        return [f"🪓 {t} — a fechadura nao cede e machuca as maos: {ev_dano(run, fid, mult=0.6)}"]

    return {"titulo": "📦 **um bau velho e trancado** repousa no canto da sala.",
            "opcoes": [_op("abrir", "abrir de uma vez (pode ter armadilha)", abrir),
                       _op("examinar", "examinar antes (teste de sab)", examinar),
                       _op("arrombar", "arrombar na forca (teste de for, pode machucar)", arrombar),
                       _op("ignorar", "deixar o bau para la", ignorar)]}


def evento_armadilha(run):
    def desarmar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "precisao")
        if ok:
            return [f"🛠️ {t} — **{f['nome']}** desmonta a armadilha e aproveita as pecas: {ev_moedas(run, fid, 0.6)}"]
        return [f"🛠️ {t} — um clique errado e os dardos disparam: {ev_dano(run, fid)}"]

    def correr(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "agilidade")
        if ok:
            return [f"💨 {t} — **{f['nome']}** atravessa ileso e trava o mecanismo do outro lado."]
        return [f"💨 {t} — os dardos acertam em cheio: {ev_dano(run, fid, mult=1.3)}"]

    def contornar(run, fid):
        return ["🧭 o explorador contorna a armadilha com cuidado e segue ileso pelo caminho longo."]

    return {"titulo": "⚠️ **placas de pressao e furos nas paredes**: uma armadilha de dardos bloqueia o corredor.",
            "opcoes": [_op("desarmar", "desarmar com cuidado (teste de pre)", desarmar),
                       _op("correr", "atravessar correndo (teste de agi)", correr),
                       _op("contornar", "dar a volta com cuidado (seguro)", contornar)]}


def evento_altar(run):
    tier = indice_dif(run)
    custo = round((10 + 3 * run["nivel"]) * mult_dif(run)["moedas"])

    def rezar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "sabedoria")
        if ok:
            b = 2 + tier
            efeitos = [ev_status(pid, "Bencao do Altar", {"combate": b, "precisao": b}, 4)
                       for pid in run["jogadores"]]
            return [f"🙏 {t} — uma luz morna envolve o grupo: " + " • ".join(efeitos)]
        efeitos = [ev_status(pid, "Maldicao do Altar", {"agilidade": -(1 + tier)}, 3)
                   for pid in run["jogadores"]]
        return [f"🙏 {t} — as runas ficam vermelhas e amaldiçoam o grupo: " + " • ".join(efeitos)]

    def oferecer(run, fid):
        membros = list(run["jogadores"])
        partes = [custo // len(membros) + (i < custo % len(membros)) for i in range(len(membros))]
        for pid, parte in zip(membros, partes):
            if get_ficha_id(pid)["moedas"] < parte:
                raise EventoInvalido(f"cada ficha precisa contribuir com ate {parte} 💰 para dividir a oferenda.")
        for pid, parte in zip(membros, partes):
            if parte:
                ev_pagar(pid, parte)
        curas = []
        for pid in membros:
            curas.append(ev_cura(pid, "corpo", 25))
            curas.append(ev_cura(pid, "mente", 25))
        pagamento = " • ".join(f"{get_ficha_id(pid)['nome']}: −{parte} 💰"
                               for pid, parte in zip(membros, partes) if parte)
        return [f"🪙 o grupo oferece {custo} 💰 ({pagamento}). o altar cura todos: " + " • ".join(curas)]

    def profanar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "forca")
        if ok:
            ganhos = [f"**{get_ficha_id(pid)['nome']}** {ev_moedas(run, pid, 2.0)}" for pid in run["jogadores"]]
            return [f"💎 {t} — **{f['nome']}** arranca as gemas e divide o tesouro: " + " • ".join(ganhos)]
        danos = [ev_dano(run, pid, "mente", 1.2) for pid in run["jogadores"]]
        return [f"👁️ {t} — uma voz invade a mente do grupo: " + " • ".join(danos)]

    return {"titulo": f"🕯️ **um altar antigo** com runas que brilham fraco. uma tigela pede oferendas ({custo} 💰).",
            "opcoes": [_op("rezar", "rezar (teste de sab: bencao ou maldicao)", rezar),
                       _op("oferecer", f"oferecer {custo} 💰 (cura 25% de corpo e mente do grupo)", oferecer),
                       _op("profanar", "arrancar as gemas (teste de for, risco mental)", profanar),
                       _op("ignorar", "nao mexer no altar", ignorar)]}


def evento_fonte(run):
    def beber(run, fid):
        if random.random() < 0.6:
            return [f"💧 a agua e revigorante: {ev_cura(fid, 'corpo', 30)}"]
        return [f"🤢 a agua e amarga e deixa a cabeca girando: {ev_dano(run, fid, 'mente', 0.8)}"]

    def analisar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "inteligencia")
        if ok:
            cura = ev_cura(fid, "corpo", 15)
            mente = ev_cura(fid, "mente", 15)
            return [f"🧪 {t} — **{f['nome']}** reconhece a agua benta e recupera: {cura} • {mente}"]
        return [f"🧪 {t} — nao da para saber o que e. melhor nao arriscar."]

    def encher(run, fid):
        if random.random() < 0.5:
            return [f"🫙 o frasco se enche de um liquido brilhante: {ev_item(run, fid, it=CATALOGO['Pocao de Cura'])}"]
        return ["🫙 o frasco racha na hora e a agua escorre pelos dedos."]

    return {"titulo": "⛲ **uma fonte de agua brilhante** borbulha no meio da sala.",
            "opcoes": [_op("beber", "beber direto (60% cura, 40% dano mental)", beber),
                       _op("analisar", "analisar a agua (teste de int: cura pessoal)", analisar),
                       _op("encher", "encher um frasco (50% de virar pocao)", encher),
                       _op("ignorar", "seguir sem beber", ignorar)]}


def evento_mercador(run):
    cons = gerar_consumivel(run["dif"])
    equip = gerar_equip(run["dif"], bonus_tier=1 if random.random() < 0.3 else 0)
    p_cons = max(1, round(CATALOGO[cons["nome"]]["preco"] * 0.7))
    p_equip = max(1, round(CATALOGO[equip["nome"]]["preco"] * 0.8))

    def compra(it, preco):
        def efeito(run, fid):
            ev_pagar(fid, preco)
            return [f"🧙 negocio fechado por {preco} 💰: {ev_item(run, fid, it=it)}"]
        return efeito

    def barganhar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "carisma")
        if ok:
            return [f"🗣️ {t} — o mercador ri e da um brinde: {ev_item(run, fid, it=cons)}"]
        return [f"🗣️ {t} — o mercador se ofende, recolhe tudo e some na escuridao."]

    return {"titulo": "🧙 **um mercador encapuzado** abre a mochila no meio do acampamento.",
            "opcoes": [_op("pocao", f"comprar **{cons['nome']}** por {p_cons} 💰 (30% off)", compra(cons, p_cons)),
                       _op("equipamento", f"comprar **{equip['nome']}** [{fmt_bonus(equip['bonus'])}] por {p_equip} 💰",
                           compra(equip, p_equip)),
                       _op("barganhar", f"pedir a {cons['nome']} de graca (teste de car)", barganhar),
                       _op("ignorar", "dispensar o mercador", ignorar)]}


def evento_prisioneiro(run):
    honesto = random.random() < 0.65

    def recompensa(run, fid):
        return f"{ev_moedas(run, fid, 0.8)} • {ev_item(run, fid, 0.3)}"

    def libertar(run, fid):
        if honesto:
            return [f"⛓️ as correntes caem e o aventureiro agradece com o que tinha escondido: {recompensa(run, fid)}"]
        return [f"🗡️ era uma isca! cultistas saltam das sombras antes de fugir: {ev_dano(run, fid, mult=1.2)}"]

    def conversar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "carisma")
        if not ok:
            return [f"💬 {t} — ele se cala, desconfiado. o grupo segue sem ele."]
        if honesto:
            return [f"💬 {t} — a historia confere. **{f['nome']}** o liberta: {recompensa(run, fid)}"]
        return [f"💬 {t} — **{f['nome']}** percebe a mentira e acha o esconderijo dos cultistas: {ev_moedas(run, fid)}"]

    return {"titulo": "⛓️ **um aventureiro acorrentado** implora por ajuda.",
            "opcoes": [_op("libertar", "soltar as correntes (pode ser uma isca)", libertar),
                       _op("conversar", "conversar primeiro (teste de car)", conversar),
                       _op("ignorar", "deixar ele la", ignorar)]}


def evento_bifurcacao(run):
    def iluminado(run, fid):
        curas = [ev_cura(pid, "corpo", 10) for pid in run["jogadores"]]
        return ["🕯️ caminho tranquilo; o grupo recupera o folego: " + " • ".join(curas)]

    def escuro(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "agilidade")
        if ok:
            premios = []
            for pid in run["jogadores"]:
                premios.append(f"**{get_ficha_id(pid)['nome']}** {ev_moedas(run, pid, 0.8)}")
                premios.append(f"item: {ev_item(run, pid, 0.2)}")
            return [f"🌑 {t} — **{f['nome']}** guia o grupo em silencio e todos encontram um tesouro: "
                    + " • ".join(premios)]
        run["extra_inimigo"] = run.get("extra_inimigo", 0) + 1
        danos = [ev_dano(run, pid, mult=0.8) for pid in run["jogadores"]]
        return [f"🌑 {t} — o grupo se perde: " + " • ".join(danos) + " e **+1 inimigo** no proximo andar."]

    def explorar(run, fid):
        f = get_ficha_id(fid)
        ok, t = teste_evento(run, f, "sabedoria")
        if ok:
            run["menos_inimigo"] = run.get("menos_inimigo", 0) + 1
            return [f"🗺️ {t} — **{f['nome']}** acha uma passagem secreta: **-1 inimigo** no proximo andar."]
        danos = [ev_dano(run, pid, "mente", 0.5) for pid in run["jogadores"]]
        return [f"🗺️ {t} — o grupo se perde e volta exausto: " + " • ".join(danos)]

    return {"titulo": "🔀 **o corredor se divide**: um caminho iluminado e um tunel escuro.",
            "opcoes": [_op("iluminado", "ir pelo caminho iluminado (seguro, cura 10% do grupo)", iluminado),
                       _op("escuro", "arriscar o tunel escuro (teste de agi: tesouro ou emboscada)", escuro),
                       _op("explorar", "procurar uma passagem secreta (teste de sab: -1 inimigo)", explorar)]}


def evento_acampamento(run):
    def descansar(run, fid):
        curas = [ev_cura(pid, "corpo", 15) for pid in run["jogadores"]]
        curas += [ev_cura(pid, "mente", 15) for pid in run["jogadores"]]
        return ["🔥 o grupo monta um acampamento seguro e recupera 15% de corpo e mente: "
                + " • ".join(curas)]

    def vasculhar(run, fid):
        f = get_ficha_id(fid)
        ok, texto = teste_evento(run, f, "sabedoria")
        if ok:
            premios = []
            for pid in run["jogadores"]:
                premios.append(f"**{get_ficha_id(pid)['nome']}** {ev_moedas(run, pid, 0.8)}")
                premios.append(f"item: {ev_item(run, pid, 0.2)}")
            return [f"🔎 {texto} — **{f['nome']}** encontra suprimentos para o grupo: " + " • ".join(premios)]
        danos = [ev_dano(run, pid, mult=0.7) for pid in run["jogadores"]]
        return [f"🔎 {texto} — a busca demora e o grupo se machuca: " + " • ".join(danos)]

    def fazer_barulho(run, fid):
        run["extra_inimigo"] = run.get("extra_inimigo", 0) + 1
        curas = [ev_cura(pid, "corpo", 25) for pid in run["jogadores"]]
        return ["🪵 o grupo prepara uma fogueira grande: recupera 25% de corpo, mas o barulho atrai "
                "**+1 inimigo** no proximo andar. " + " • ".join(curas)]

    return {"titulo": "🔥 **um acampamento abandonado** ainda tem brasas sob as cinzas.",
            "opcoes": [_op("descansar", "descansar com seguranca (cura 15% de corpo e mente do grupo)", descansar),
                       _op("vasculhar", "vasculhar as mochilas (teste de sab: premio ou dano)", vasculhar),
                       _op("fogueira", "acender uma fogueira (cura 25% do grupo, +1 inimigo)", fazer_barulho)]}


def evento_ponte(run):
    def reparar(run, fid):
        f = get_ficha_id(fid)
        ok, texto = teste_evento(run, f, "inteligencia")
        if ok:
            ganhos = [f"**{get_ficha_id(pid)['nome']}** {ev_moedas(run, pid, 0.7)}" for pid in run["jogadores"]]
            return [f"🧰 {texto} — **{f['nome']}** reforca as cordas; o grupo atravessa e recupera equipamento: "
                    + " • ".join(ganhos)]
        danos = [ev_dano(run, pid, mult=0.9) for pid in run["jogadores"]]
        return [f"🧰 {texto} — a corda se rompe e o grupo se fere: " + " • ".join(danos)]

    def saltar(run, fid):
        f = get_ficha_id(fid)
        ok, texto = teste_evento(run, f, "agilidade")
        if ok:
            run["menos_inimigo"] = run.get("menos_inimigo", 0) + 1
            return [f"💨 {texto} — **{f['nome']}** acha uma passagem estreita e evita uma emboscada "
                    "(−1 inimigo no proximo andar)."]
        danos = [ev_dano(run, pid, mult=1.1) for pid in run["jogadores"]]
        return [f"💨 {texto} — a travessia falha e o grupo se fere: " + " • ".join(danos)]

    def voltar(run, fid):
        curas = [ev_cura(pid, "mente", 10) for pid in run["jogadores"]]
        run["menos_inimigo"] = run.get("menos_inimigo", 0) + 1
        return ["↩️ o grupo procura outro caminho. A travessia leva tempo, mas evita uma emboscada "
                "(−1 inimigo no proximo andar) e recupera 10% de mente: " + " • ".join(curas)]

    return {"titulo": "🌉 **uma ponte suspensa** balanca sobre um abismo profundo.",
            "opcoes": [_op("reparar", "reforcar a ponte (teste de int: moedas ou dano)", reparar),
                       _op("saltar", "pular pelas plataformas (teste de agi: atalho ou dano)", saltar),
                       _op("voltar", "procurar outra rota (seguro, −1 inimigo e cura 10% de mente)", voltar)]}


def evento_biblioteca(run):
    def estudar(run, fid):
        f = get_ficha_id(fid)
        ok, texto = teste_evento(run, f, "inteligencia")
        if ok:
            b = 2 + indice_dif(run)
            return [f"📚 {texto} — **{f['nome']}** decifra os pergaminhos: "
                    + ev_status(fid, "Estudo da Biblioteca", {"inteligencia": b, "sabedoria": b}, 3)]
        return [f"📚 {texto} — um selo estoura e **{f['nome']}** perde energia: "
                f"{ev_dano(run, fid, 'mente', 0.8)}"]

    def saquear(run, fid):
        f = get_ficha_id(fid)
        ok, texto = teste_evento(run, f, "precisao")
        if ok:
            return [f"🗝️ {texto} — **{f['nome']}** abre um cofre oculto: "
                    f"{ev_moedas(run, fid, 1.2)} • {ev_item(run, fid, 0.5)}"]
        return [f"🗝️ {texto} — uma armadilha dispara: {ev_dano(run, fid, 'corpo', 0.8)}"]

    def ignorar_sala(run, fid):
        f = get_ficha_id(fid)
        return [f"🚪 **{f['nome']}** evita os selos e segue em frente, recuperando "
                f"{ev_cura(fid, 'mente', 10)}."]

    return {"titulo": "📚 **uma biblioteca lacrada** guarda grimorios e cofres antigos.",
            "opcoes": [_op("estudar", "estudar os grimorios (teste de int: bonus ou dano mental pessoal)", estudar),
                       _op("saquear", "abrir o cofre (teste de pre: moedas e item ou dano)", saquear),
                       _op("ignorar", "evitar os selos (cura 10% de mente pessoal)", ignorar_sala)]}


def evento_caravana(run):
    preco = max(1, round((8 + 2 * run["nivel"]) * mult_dif(run)["moedas"]))

    def comprar(run, fid):
        ev_pagar(fid, preco)
        item = gerar_consumivel(run["dif"])
        return [f"🛒 pagamento de {preco} 💰 confirmado: {ev_item(run, fid, it=item)}."]

    def negociar(run, fid):
        f = get_ficha_id(fid)
        ok, texto = teste_evento(run, f, "carisma")
        if ok:
            return [f"🤝 {texto} — o comerciante aceita a troca e **{f['nome']}** recebe "
                    f"{ev_moedas(run, fid, 1.0)}."]
        return [f"🤝 {texto} — o comerciante se irrita e vai embora."]

    def seguir(run, fid):
        return [f"🧭 o explorador acompanha a caravana a distancia e recupera "
                f"{ev_cura(fid, 'mente', 10)} durante a caminhada."]

    return {"titulo": "🐪 **uma caravana de exploradores** oferece ajuda por um preco.",
            "opcoes": [_op("comprar", f"comprar suprimentos por {preco} 💰 (consumível)", comprar),
                       _op("negociar", "negociar uma recompensa (teste de car: moedas ou nada)", negociar),
                       _op("seguir", "acompanhar a caravana (cura pessoal de mente)", seguir)]}


EVENTOS = [
    (evento_bau, 3, "solo"),
    (evento_armadilha, 2, "solo"),
    (evento_altar, 2, "grupo"),
    (evento_fonte, 2, "solo"),
    (evento_mercador, 2, "solo"),
    (evento_prisioneiro, 1, "solo"),
    (evento_bifurcacao, 2, "grupo"),
    (evento_acampamento, 2, "grupo"),
    (evento_ponte, 2, "grupo"),
    (evento_biblioteca, 2, "solo"),
    (evento_caravana, 1, "solo"),
]


def criar_evento(run):
    """sorteia um evento e um explorador que executa testes individuais."""
    vivos = list(run["jogadores"])
    if not vivos:
        return None
    menor = min(run["resumo"][fid]["eventos"] for fid in vivos)
    fid = random.choice([x for x in vivos if run["resumo"][x]["eventos"] == menor])
    run["resumo"][fid]["eventos"] += 1
    fabrica, _, escopo = random.choices(EVENTOS, weights=[peso for _, peso, _ in EVENTOS])[0]
    ev = fabrica(run)
    ev["fid"] = fid
    ev["escopo"] = escopo
    run["evento"] = ev
    return texto_evento(run)


def texto_evento(run) -> str:
    ev = run["evento"]
    f = get_ficha_id(ev["fid"])
    if ev["fid"] not in run["jogadores"]:
        ev["fid"] = next(iter(run["jogadores"]))
        f = get_ficha_id(ev["fid"])
    opcoes_voto = ev.get("opcoes_voto")
    permitidas = set(opcoes_voto) if opcoes_voto else None
    ops = "\n".join(
        f"`{i}` **{o['chave']}** — {o['rotulo']}"
        for i, o in enumerate(ev["opcoes"], 1)
        if permitidas is None or o["chave"] in permitidas
    )
    if ev.get("escopo") == "solo":
        return (f"🎲 **evento individual!** {ev['titulo']}\n"
                f"👤 **{f['nome'] if f else '?'}** decide sozinho; o efeito e pessoal.\n"
                f"{ops}\nresponda com `!escolher [numero ou nome]`.")
    votos = ev.setdefault("votos", {})
    contagem = {o["chave"]: 0 for o in ev["opcoes"] if permitidas is None or o["chave"] in permitidas}
    for escolha in votos.values():
        if escolha in contagem:
            contagem[escolha] += 1
    total_votaram = sum(contagem.values())
    return (f"🎲 **evento!** {ev['titulo']}\n"
            f"👥 **evento de grupo:** a opcao escolhida afeta todos. "
            f"{f['nome'] if f else '?'} faz o teste quando a opcao exigir.\n"
            "🗳️ todos votam; a opcao mais votada vence.\n"
            f"{ops}\n"
            f"votos ({total_votaram}/{len(run['jogadores'])}): "
            + " • ".join(f"{chave}: {qtd}" for chave, qtd in contagem.items())
            + "\nuse `!escolher [numero ou nome]`. seu voto pode ser alterado ate todos votarem.")


def registrar_voto_evento(ev, participantes, ficha_id, escolha):
    """registra um voto; quando todos votam retorna o vencedor ou as opcoes empatadas."""
    votos = ev.setdefault("votos", {})
    voto_anterior = votos.get(ficha_id)
    votos = {fid: voto for fid, voto in votos.items() if fid in participantes}
    votos[ficha_id] = escolha
    ev["votos"] = votos
    if not participantes.issubset(votos):
        return voto_anterior, False, []

    contagem = {}
    for voto in votos.values():
        contagem[voto] = contagem.get(voto, 0) + 1
    maior = max(contagem.values())
    vencedoras = [chave for chave, quantidade in contagem.items() if quantidade == maior]
    if len(vencedoras) > 1:
        ev["opcoes_voto"] = vencedoras
        ev["votos"] = {}
        ev["rodada_votacao"] = ev.get("rodada_votacao", 1) + 1
        return voto_anterior, True, vencedoras
    return voto_anterior, True, vencedoras


# comandos: lobby
async def escolher_ficha(ctx, texto):
    """ficha do proprio jogador: nome, comeco do nome ou ordem (#2); sem nome vale a #1."""
    minhas = fichas_do_usuario(ctx.guild.id, ctx.author.id)
    if not minhas:
        await ctx.send("voce ainda nao tem ficha. use `!criar [nome]`.")
        return None
    if not texto:
        return minhas[0]
    f = ficha_por_token(ctx, texto)
    if not f:
        await ctx.send(msg_ficha_nao_achada(ctx.guild.id, texto))
        return None
    if f["dono_id"] != ctx.author.id:
        await ctx.send("🚫 voce so pode entrar numa dungeon com uma ficha sua.")
        return None
    return f


async def validar_entrada(ctx, f) -> bool:
    if f["corpo"] <= 0:
        await ctx.send(f"**{f['nome']}** esta com corpo em 0.")
        return False
    if run_do_usuario(ctx.guild.id, ctx.author.id):
        await ctx.send("voce ja esta em uma dungeon.")
        return False
    if run_da_ficha(f["id"]):
        await ctx.send(f"**{f['nome']}** ja esta em uma dungeon.")
        return False
    return True


AVISO_MORTE = ("se **{nome}** (nivel {nivel}) chegar a corpo 0 na dungeon, a ficha e **apagada para sempre** "
               "(nao da para restaurar). entrar mesmo assim?")


def texto_lobby(run) -> str:
    d = mult_dif(run)
    nomes = ", ".join(f"**{run['resumo'][fid]['nome']}** (<@{p['user']}>)" for fid, p in run["jogadores"].items())
    return (f"🏰 **{TEMAS[run['tema']]['nome']}** — dificuldade **{run['dif']}** • "
            f"{d['andares']} andares\n"
            f"grupo ({len(run['jogadores'])}/{MAX_JOGADORES}): {nomes}\n"
            "mais gente entra com `!dungeon entrar [ficha]`; o lider comeca com `!dungeon iniciar` (pode ser solo). "
            "`!dungeon sair` sai do grupo.")


async def dg_criar(ctx, tokens):
    if ctx.channel.id in DUNGEONS:
        await ctx.send("ja existe uma dungeon neste canal. veja com `!dungeon status`.")
        return
    dif, tema, ficha_txt = None, None, None
    for t, _ in tokens:
        n = norm(t)
        if achar_dif(t) and dif is None:
            dif = achar_dif(t)
        elif n in TEMAS:
            tema = n
        elif ficha_txt is None:
            ficha_txt = t
        else:
            await ctx.send("uso: `!dungeon criar [facil|medio|dificil|pesadelo] [caverna|cripta|floresta|ruinas] [ficha]`")
            return
    f = await escolher_ficha(ctx, ficha_txt)
    if not f or not await validar_entrada(ctx, f):
        return
    dif = dif or dif_do_nivel(min(20, nivel_efetivo(f)))
    tema = tema or random.choice(list(TEMAS))
    if not await confirmar(ctx, AVISO_MORTE.format(nome=f["nome"], nivel=fmt_nivel(f["nivel"]))):
        return
    f = get_ficha_id(f["id"])
    if f is None or ctx.channel.id in DUNGEONS or not await validar_entrada(ctx, f):
        return
    run = {"canal": ctx.channel.id, "guild": ctx.guild.id, "lider": ctx.author.id, "dif": dif, "tema": tema,
           "estado": "lobby", "jogadores": {}, "resumo": {}, "inimigos": [], "ordem": [], "pos": 0, "rodada": 1,
           "andar": 0, "nivel": 1, "contrib": {}, "ultima": time.time(), "lock": asyncio.Lock()}
    novo_jogador(run, f, ctx.author.id)
    DUNGEONS[ctx.channel.id] = run
    await ctx.send(texto_lobby(run))


async def obter_run(ctx):
    run = DUNGEONS.get(ctx.channel.id)
    if run is None:
        await ctx.send("nao ha dungeon neste canal. crie uma com `!dungeon criar`.")
    return run


async def dg_entrar(ctx, tokens):
    run = await obter_run(ctx)
    if not run:
        return
    if run["estado"] != "lobby":
        await ctx.send("a dungeon ja comecou; so da para entrar antes do inicio.")
        return
    if len(run["jogadores"]) >= MAX_JOGADORES:
        await ctx.send(f"o grupo ja tem {MAX_JOGADORES} jogadores.")
        return
    f = await escolher_ficha(ctx, tokens[0][0] if tokens else None)
    if not f or not await validar_entrada(ctx, f):
        return
    if not await confirmar(ctx, AVISO_MORTE.format(nome=f["nome"], nivel=fmt_nivel(f["nivel"]))):
        return
    async with run["lock"]:
        f = get_ficha_id(f["id"])
        if DUNGEONS.get(ctx.channel.id) is not run or run["estado"] != "lobby" \
                or len(run["jogadores"]) >= MAX_JOGADORES or f is None or not await validar_entrada(ctx, f):
            return
        novo_jogador(run, f, ctx.author.id)
        run["ultima"] = time.time()
        await ctx.send(f"✅ **{f['nome']}** entrou no grupo.\n" + texto_lobby(run))


async def dg_iniciar(ctx):
    run = await obter_run(ctx)
    if not run:
        return
    async with run["lock"]:
        if ctx.author.id != run["lider"]:
            await ctx.send(f"so o lider (<@{run['lider']}>) pode iniciar.")
            return
        if run["estado"] != "lobby":
            await ctx.send("a dungeon ja comecou.")
            return
        run["ultima"] = time.time()
        await iniciar_andar(ctx, run)


async def dg_avancar(ctx):
    run = await obter_run(ctx)
    if not run:
        return
    async with run["lock"]:
        if ctx.author.id != run["lider"]:
            await ctx.send(f"so o lider (<@{run['lider']}>) pode avancar.")
            return
        if run["estado"] != "pausa":
            await ctx.send("so da para avancar no acampamento, entre os andares.")
            return
        if run.get("evento"):
            await ctx.send("ainda ha uma decisao pendente:\n" + texto_evento(run))
            return
        run["ultima"] = time.time()
        await iniciar_andar(ctx, run)


async def dg_status(ctx):
    run = await obter_run(ctx)
    if not run:
        return
    if run["estado"] == "lobby":
        await ctx.send(texto_lobby(run))
        return
    d = mult_dif(run)
    cab = (f"🏰 **{TEMAS[run['tema']]['nome']}** ({run['dif']}) — andar {run['andar']}/{d['andares']} • "
           f"{'combate' if run['estado'] == 'combate' else 'acampamento'}")
    corpo = texto_painel(run)
    if run["estado"] == "combate":
        corpo += f"\n{texto_vez(run)}"
    elif run.get("evento"):
        corpo += "\n\n" + texto_evento(run)
    await enviar_longo(ctx, cab + "\n" + corpo)


async def dg_ranking(ctx):
    run = await obter_run(ctx)
    if not run:
        return
    await enviar_longo(ctx, texto_ranking(run))


async def dg_sair(ctx):
    run = await obter_run(ctx)
    if not run:
        return
    async with run["lock"]:
        fid = jogador_do_usuario(run, ctx.author.id)
        if fid is None:
            await ctx.send("voce nao esta nessa dungeon.")
            return
        if run["estado"] == "combate":
            await ctx.send("em combate nao da para sair da dungeon: `!fugir` so foge da batalha. "
                           "saia no acampamento com `!dungeon sair`.")
            return
        nome = run["resumo"][fid]["nome"]
        run["resumo"][fid]["fim"] = "saiu com vida"
        del run["jogadores"][fid]
        run["ultima"] = time.time()
        if not run["jogadores"]:
            await fim_dungeon(ctx, run, "cancelada" if run["estado"] == "lobby" else "saiu")
            return
        if run["lider"] == ctx.author.id:
            run["lider"] = next(iter(run["jogadores"].values()))["user"]
        ev = run.get("evento")
        if ev:
            ev.setdefault("votos", {}).pop(fid, None)
            if ev["fid"] == fid:
                ev["fid"] = next(iter(run["jogadores"]))
        await ctx.send(f"🚪 **{nome}** saiu da dungeon. lider: <@{run['lider']}>."
                       + ("\n" + texto_evento(run) if ev else ""))


@bot.command(name="dungeon")
async def dungeon(ctx, *, raw: str = ""):
    tokens = tokenizar(raw)
    sub = norm(tokens[0][0]) if tokens else ""
    resto = tokens[1:]
    if sub == "criar":
        await dg_criar(ctx, resto)
    elif sub == "entrar":
        await dg_entrar(ctx, resto)
    elif sub == "iniciar":
        await dg_iniciar(ctx)
    elif sub == "avancar":
        await dg_avancar(ctx)
    elif sub == "status":
        await dg_status(ctx)
    elif sub == "sair":
        await dg_sair(ctx)
    elif sub == "ranking":
        await dg_ranking(ctx)
    else:
        dificuldades = "\n".join(
            f"• **{k.title()}** · níveis {v['niveis'][0]}–{v['niveis'][1]} · {v['andares']} andares\n"
            f"  Inimigos: {int(v['fator'] * 100)}% da força de referência · "
            f"XP: ×{v['xp']} · moedas: ×{v['moedas']} · saque comum: {int(v['chance_loot'] * 100)}%"
            for k, v in DIFICULDADES.items()
        )
        temas = " · ".join(f"`{k}` — {tema['nome']}" for k, tema in TEMAS.items())
        await enviar_longo(ctx, (
            "🏰 **Dungeons**\n"
            "Explore sozinho ou em um grupo de até 4 jogadores. A dungeon fica no canal em que foi criada.\n\n"
            "**Comandos**\n"
            "• Criar: `!dungeon criar [dificuldade] [tema] [ficha]`\n"
            "• Entrar no grupo: `!dungeon entrar [ficha]`\n"
            "• Iniciar ou avançar: `!dungeon iniciar` · `!dungeon avancar`\n"
            "• Consultar: `!dungeon status` · `!dungeon ranking`\n"
            "• Sair no lobby ou acampamento: `!dungeon sair`\n\n"
            "**Dificuldades sugeridas**\n"
            f"{dificuldades}\n\n"
            f"**Temas disponíveis:** {temas}\n\n"
            "⚠️ **Risco permanente:** se o corpo chegar a 0, a ficha será apagada e não poderá ser restaurada.\n"
            "Cada andar vencido concede XP, moedas e uma chance de encontrar itens. O último andar tem um chefe; "
            "ele sempre deixa equipamento.\n\n"
            "**Eventos e ranking**\n"
            "No acampamento podem surgir eventos. Nos eventos de grupo, todos votam; nos individuais, "
            "a escolha cabe ao explorador indicado. Use `!escolher` para votar ou decidir.\n"
            "Quem acumular mais abates entre os participantes que concluírem a dungeon pode receber o prêmio de MVP. "
            "Consulte `!ranking` para ver o ranking do servidor.\n\n"
            "**Combate**\n"
            "Força e inteligência contribuem para o dano ao corpo. Consulte `!ajuda atacar` e `!ajuda usarh` "
            "para ver os detalhes das ações."
        ))


# comandos: acoes de combate
async def validar_vez(ctx, run):
    fid = jogador_do_usuario(run, ctx.author.id)
    if fid is None:
        await ctx.send("voce nao esta nessa dungeon.")
        return None
    if run["estado"] != "combate":
        await ctx.send("nao ha combate agora.")
        return None
    if run["ordem"][run["pos"]] != ("j", fid):
        await ctx.send(f"ainda nao e a sua vez: {texto_vez(run)}")
        return None
    run["ultima"] = time.time()
    return fid


@bot.command(name="atacar")
async def atacar(ctx, *, raw: str = ""):
    run = await obter_run(ctx)
    if not run:
        return
    async with run["lock"]:
        fid = await validar_vez(ctx, run)
        if fid is None:
            return
        f = get_ficha_id(fid)
        vivos = [(i, m) for i, m in enumerate(run["inimigos"]) if inimigo_vivo(m)]
        tokens = tokenizar(raw)
        modo_pos = next((i for i, (t, _) in enumerate(tokens) if norm(t) in MODOS_ATAQUE), None)
        if modo_pos is None or modo_pos == 0 or modo_pos < 0:
                await ctx.send("uso: `!atacar [alvo] [perto|longe|magia|jujutsu] [habilidade opcional]`.")
                return
        alvo_txt = " ".join(t for t, _ in tokens[:modo_pos]).strip().strip(ASPAS).strip()
        modo = norm(tokens[modo_pos][0])
        acerto, dano = MODOS_ATAQUE[modo]
        alvo_num = re.fullmatch(r"#?(\d+)", alvo_txt)
        if alvo_num:
                alvo_i = int(alvo_num.group(1)) - 1
                if not 0 <= alvo_i < len(run["inimigos"]) or not inimigo_vivo(run["inimigos"][alvo_i]):
                    await ctx.send("esse inimigo nao existe ou ja caiu. veja os numeros no painel.")
                    return
        else:
                alvo = achar_nome([m for _, m in vivos], alvo_txt)
                if not alvo:
                    await ctx.send(f"nao encontrei o inimigo **{alvo_txt}**. use o numero ou nome exibido no painel.")
                    return
                alvo_i = run["inimigos"].index(alvo)
        nome_hab_txt = " ".join(t for t, _ in tokens[modo_pos + 1:]).strip().strip(ASPAS).strip()
        hab = get_habilidade(fid, nome_hab_txt) if nome_hab_txt else None
        if nome_hab_txt and not hab:
                await ctx.send(msg_nao_tem(f, "habilidade", nome_hab_txt))
                return
        m = run["inimigos"][alvo_i]
        ba = bd = 0
        nome_hab = None
        if hab:
                if hab["modo"] in MODOS_ATAQUE and hab["modo"] != modo:
                    await ctx.send(f"**{hab['nome']}** e do tipo **{nome_tipo_habilidade(hab['modo'])}**; "
                                   "use esse tipo em `!atacar`.")
                    return
                bonus = json.loads(hab["bonus"])
                nome_hab = hab["nome"]
                if hab["ativa"]:
                    nome_hab += " (ja ativa)"
                else:
                    ba = valor_bonus(bonus.get(acerto, 0), f[acerto])
                    if isinstance(dano, tuple) and isinstance(dano[1], tuple):
                        principal, (fracionado, divisor) = dano
                        bd = valor_bonus(bonus.get(principal, 0), f[principal])
                        bd += valor_bonus(bonus.get(fracionado, 0), f[fracionado]) // divisor
                    elif isinstance(dano, tuple):
                        bd = sum(valor_bonus(bonus.get(attr, 0), f[attr]) for attr in dano) // 2
                    else:
                        bd = valor_bonus(bonus.get(dano, 0), f[dano])
                    if not ba and not bd:
                        if isinstance(dano, tuple) and isinstance(dano[1], tuple):
                            attrs_dano = f"{ATRIBUTOS[dano[0]]} e {ATRIBUTOS[dano[1][0]]}/{dano[1][1]}"
                        else:
                            attrs_dano = "/".join(ATRIBUTOS[attr] for attr in dano) if isinstance(dano, tuple) else ATRIBUTOS[dano]
                        await ctx.send(f"**{hab['nome']}** nao altera {ATRIBUTOS[acerto]} nem {attrs_dano}. "
                                       "se ela so cura (crp/mnt), use `!usarh`.")
                        return
        linhas = acao_ataque(f, m, acerto, dano, nome_hab, ba, bd,
                             reg=run["contrib"].setdefault(fid, {"dano": 0, "acertos": 0, "abates": 0}),
                             tot=run["resumo"][fid])
        await fim_da_acao(ctx, run, fid, linhas)


@bot.command(name="usar")
async def usar(ctx, *, raw: str = ""):
    run = await obter_run(ctx)
    if not run:
        return
    async with run["lock"]:
        fid = await validar_vez(ctx, run)
        if fid is None:
            return
        nome_txt, alvo_ref = separar_alvo_dungeon(run, raw, inimigos=False)
        nome = nome_txt.strip().strip(ASPAS).strip()
        if not nome:
            await ctx.send("uso: `!usar [consumível] [alvo aliado opcional]` (gasta o seu turno)")
            return
        f = get_ficha_id(fid)
        item, hab = get_item(fid, nome), get_habilidade(fid, nome)
        alvo = get_ficha_id(alvo_ref[1]) if alvo_ref else f
        if alvo_ref and (alvo_ref[0] != "j" or alvo is None):
            await ctx.send("itens so podem ser usados em fichas do seu grupo na dungeon.")
            return
        if alvo_ref and run["jogadores"][alvo_ref[1]].get("fugiu"):
            await ctx.send("esse aliado ja fugiu da batalha e nao pode ser alvo agora.")
            return
        if item and item["tipo"] == "consumivel":
            await aplicar_item_consumivel(ctx, f, item, alvo)
        elif hab:
            await ctx.send(f"use `!usarh {hab['nome']}` para ativar a habilidade durante o combate.")
            return
        elif item:
            await ctx.send(f"**{item['nome']}** e normal; equipe no acampamento com `!equipar`.")
            return
        else:
            await ctx.send(msg_nao_tem(f, "item", nome))
            return
        await fim_da_acao(ctx, run, fid, [])


@bot.command(name="descansar")
async def descansar(ctx, *, raw: str = ""):
    run = await obter_run(ctx)
    if not run:
        return
    campo = VITAIS.get(norm(raw)) if raw.strip() else "corpo"
    if campo is None:
        await ctx.send("uso: `!descansar [crp|mnt]`")
        return
    async with run["lock"]:
        if run["estado"] == "pausa":
            fid = jogador_do_usuario(run, ctx.author.id)
            if fid is None:
                await ctx.send("voce nao esta nessa dungeon.")
                return
            p = run["jogadores"][fid]
            if p["descansou"]:
                await ctx.send("voce ja descansou neste acampamento.")
                return
            divisor, lugar = 5, "no acampamento"
        else:
            fid = await validar_vez(ctx, run)
            if fid is None:
                return
            divisor, lugar = 20, "em combate"
        f = get_ficha_id(fid)
        vigor, vontade = maximos(f, {})
        maximo = vigor if campo == "corpo" else vontade
        ganho = max(1, maximo // divisor)
        novo = min(maximo, f[campo] + ganho)
        db.execute(f"UPDATE fichas SET {campo}=? WHERE id=?", (novo, fid))
        db.commit()
        run["ultima"] = time.time()
        linha = f"😮‍💨 **{f['nome']}** descansou {lugar}: {SIG[campo]} {f[campo]} → {novo}"
        if run["estado"] == "pausa":
            run["jogadores"][fid]["descansou"] = True
            await ctx.send(linha)
        else:
            await fim_da_acao(ctx, run, fid, [linha])


@bot.command(name="passar")
async def passar(ctx):
    run = await obter_run(ctx)
    if not run:
        return
    async with run["lock"]:
        fid = await validar_vez(ctx, run)
        if fid is None:
            return
        f = get_ficha_id(fid)
        await fim_da_acao(ctx, run, fid, [f"⏭️ **{f['nome']}** passou a vez."])


@bot.command(name="fugir")
async def fugir(ctx):
    run = await obter_run(ctx)
    if not run:
        return
    async with run["lock"]:
        fid = await validar_vez(ctx, run)
        if fid is None:
            return
        f = get_ficha_id(fid)
        eu = rolar_attr(f, "agilidade")
        melhor = max((rolar_monstro(m, "agilidade") for m in run["inimigos"] if inimigo_vivo(m)),
                     key=lambda r: r["total"])
        texto = f"🏃 **{f['nome']}** tenta fugir: agi {_t(eu)}×{_t(melhor)}"
        if eu["total"] < melhor["total"] - 4:
            await fim_da_acao(ctx, run, fid, [texto + " → falhou! perdeu o turno."])
            return
        run["jogadores"][fid]["fugiu"] = True
        linhas = [texto + " → **escapou da batalha!** continua na dungeon e volta no acampamento, "
                          "mas fica sem a recompensa deste andar."]
        avancar_pos(run)
        await processar_ordem(ctx, run, linhas)


@bot.command(name="escolher")
async def escolher(ctx, *, raw: str = ""):
    """registra voto em evento coletivo ou resolve a escolha individual do explorador."""
    run = await obter_run(ctx)
    if not run:
        return
    async with run["lock"]:
        ev = run.get("evento")
        if not ev or run["estado"] != "pausa":
            await ctx.send("nao ha nenhuma decisao pendente agora.")
            return
        meu = jogador_do_usuario(run, ctx.author.id)
        if meu is None:
            await ctx.send("voce nao esta nessa dungeon.")
            return
        arg = norm(raw.strip().strip(ASPAS))
        if not arg:
            await ctx.send(texto_evento(run))
            return
        op = None
        if arg.isdigit() and 1 <= int(arg) <= len(ev["opcoes"]):
            op = ev["opcoes"][int(arg) - 1]
        else:
            achado = achar_nome([{"nome": o["chave"], "i": i} for i, o in enumerate(ev["opcoes"])], arg)
            op = ev["opcoes"][achado["i"]] if achado else None
        if not op:
            await ctx.send("opcao invalida.\n" + texto_evento(run))
            return

        if ev.get("escopo") == "solo":
            fid = ev["fid"]
            if fid not in run["jogadores"]:
                fid = next(iter(run["jogadores"]))
                ev["fid"] = fid
            if meu != fid:
                ficha = get_ficha_id(fid)
                await ctx.send(f"este e um evento individual; **{ficha['nome'] if ficha else '?'}** decide sozinho.")
                return
            try:
                linhas = op["efeito"](run, fid)
            except EventoInvalido as e:
                await ctx.send(str(e))
                return
            run.pop("evento", None)
            run["ultima"] = time.time()
            ficha = get_ficha_id(fid)
            registrar_historico(fid, run["jogadores"][fid]["user"],
                                f"dungeon {run['tema']} evento individual: {op['chave']}")
            await enviar_longo(ctx, f"🎲 **{ficha['nome'] if ficha else '?'}** escolheu **{op['chave']}** "
                                    "(evento individual).\n" + "\n".join(linhas)
                                    + "\n\nquando estiverem prontos, o lider usa `!dungeon avancar`.")
            return

        opcoes_voto = ev.get("opcoes_voto")
        if opcoes_voto and op["chave"] not in opcoes_voto:
            await ctx.send("a votacao de desempate permite apenas as opcoes empatadas.\n" + texto_evento(run))
            return
        elegiveis = set(run["jogadores"])
        voto_anterior, todos_votaram, vencedoras = registrar_voto_evento(ev, elegiveis, meu, op["chave"])
        if not todos_votaram:
            aguardando = [
                f"**{get_ficha_id(fid)['nome']}**"
                for fid in elegiveis - set(ev["votos"])
                if get_ficha_id(fid)
            ]
            alterado = " (voto atualizado)" if voto_anterior else ""
            await ctx.send(f"🗳️ voto registrado{alterado}: **{op['chave']}**. "
                           f"aguardando: {', '.join(aguardando)}.\n" + texto_evento(run))
            run["ultima"] = time.time()
            return

        if len(vencedoras) > 1:
            await ctx.send(f"🗳️ empate entre {', '.join(f'**{chave}**' for chave in vencedoras)}. "
                           "todos votam novamente apenas entre essas opcoes.\n" + texto_evento(run))
            run["ultima"] = time.time()
            return

        escolha = vencedoras[0]
        op = next(opcao for opcao in ev["opcoes"] if opcao["chave"] == escolha)
        fid = ev["fid"]
        if fid not in run["jogadores"]:
            fid = next(iter(run["jogadores"]))
            ev["fid"] = fid
        try:
            linhas = op["efeito"](run, fid)
        except EventoInvalido as e:
            ev["votos"] = {}
            ev.pop("opcoes_voto", None)
            run["ultima"] = time.time()
            await ctx.send(f"{e}\n🗳️ votacao reiniciada para escolher outra opcao.\n" + texto_evento(run))
            return
        run.pop("evento", None)
        run["ultima"] = time.time()
        for participante in elegiveis:
            jogador = run["jogadores"].get(participante)
            if jogador:
                registrar_historico(participante, jogador["user"],
                                    f"dungeon {run['tema']} evento de grupo: {op['chave']}")
        await enviar_longo(ctx, f"🗳️ o grupo escolheu **{op['chave']}**.\n" + "\n".join(linhas)
                           + "\n\nquando estiverem prontos, o lider usa `!dungeon avancar`.")


CRITERIOS_RANKING = {"abates": "abates", "kills": "abates", "mortes": "abates", "mvp": "mvps", "mvps": "mvps",
                     "dano": "dano", "vitorias": "vitorias", "dungeons": "dungeons"}


@bot.command(name="ranking")
async def ranking(ctx, *, raw: str = ""):
    crit = CRITERIOS_RANKING.get(norm(raw) or "abates")
    if not crit:
        await ctx.send("uso: `!ranking [abates|mvp|dano|vitorias|dungeons]`")
        return
    rows = db.execute(f"SELECT * FROM ranking WHERE guild_id=? ORDER BY {crit} DESC, abates DESC, mvps DESC LIMIT 10",
                      (ctx.guild.id,)).fetchall()
    if not rows:
        await ctx.send("🏆 ninguem terminou uma dungeon ainda. o ranking aparece depois da primeira.")
        return
    linhas = []
    for i, r in enumerate(rows):
        icone = MEDALHAS[i] if i < 3 else f"{i + 1}."
        linhas.append(f"{icone} <@{r['user_id']}> — {r['abates']} abate(s) • {r['mvps']} mvp • {r['dano']} de dano • "
                      f"{r['vitorias']}/{r['dungeons']} dungeon(s) vencida(s)")
    await enviar_longo(ctx, f"🏆 **ranking do servidor** (por {crit})\n" + "\n".join(linhas))


# inicio
def carregar_token() -> str:
    token = os.environ.get("DISCORD_TOKEN")
    caminho = os.path.join(PASTA, "token.txt")
    if not token and os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as arq:
            token = arq.read().strip()
    if not token:
        raise SystemExit("defina a variavel DISCORD_TOKEN ou crie um arquivo token.txt com o token do bot.")
    return token


if __name__ == "__main__":
    # organiza a ordem das fichas de cada jogador (1, 2, 3...)
    for _r in db.execute("SELECT DISTINCT guild_id, dono_id FROM fichas").fetchall():
        renumerar(_r["guild_id"], _r["dono_id"])
    # corrige corpo/mente que tenham ficado acima do maximo calculado so com atributos base
    for _r in db.execute("SELECT id FROM fichas").fetchall():
        normalizar_vitais(_r["id"])
    fazer_backup()
    bot.run(carregar_token())
