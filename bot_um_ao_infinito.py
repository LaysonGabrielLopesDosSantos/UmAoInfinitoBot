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
copia de seguranca e salva como fichas.db.bak-antes-da-v3
"""

import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import traceback
import unicodedata

import discord
from discord.ext import commands

# ----------------------------------------------------------------------------
# configuracoes
# ----------------------------------------------------------------------------
PASTA = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PASTA, "fichas.db")
MAX_FICHAS = 2          # limite padrao de fichas por pessoa (admin pode mudar)
LIMITE_ATRIBUTO = 20    # pontos maximos por atributo
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
TIPOS_ITEM = {"estatico": "estatico", "consumivel": "consumivel"}


# ----------------------------------------------------------------------------
# banco de dados (com migracao automatica)
# ----------------------------------------------------------------------------
def precisa_migrar() -> bool:
    if not os.path.exists(DB_PATH):
        return False
    c = sqlite3.connect(DB_PATH)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(fichas)")]
        if cols and ("xp" not in cols or "ordem" not in cols):
            return True
        tem_status = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='status'").fetchone()
        if tem_status and c.execute("SELECT 1 FROM status WHERE tipo IN ('static', 'dynamic') LIMIT 1").fetchone():
            return True
        return False
    finally:
        c.close()


if precisa_migrar():
    shutil.copy2(DB_PATH, DB_PATH + ".bak-antes-da-v3")
    print("copia de seguranca salva em fichas.db.bak-antes-da-v3")

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")
db.executescript("""
CREATE TABLE IF NOT EXISTS fichas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    dono_id INTEGER NOT NULL,
    nome TEXT NOT NULL COLLATE NOCASE,
    nivel INTEGER NOT NULL DEFAULT 1,
    xp INTEGER NOT NULL DEFAULT 0,
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
    UNIQUE (ficha_id, nome)
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
""")


def garantir_coluna(tabela, coluna, definicao):
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({tabela})")]
    if coluna not in cols:
        db.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")


garantir_coluna("fichas", "xp", "INTEGER NOT NULL DEFAULT 0")
garantir_coluna("itens", "tipo", "TEXT NOT NULL DEFAULT 'estatico'")
garantir_coluna("fichas", "ordem", "INTEGER NOT NULL DEFAULT 0")
db.execute("UPDATE status SET tipo='estatico' WHERE tipo='static'")
db.execute("UPDATE status SET tipo='dinamico' WHERE tipo='dynamic'")
db.commit()


# ----------------------------------------------------------------------------
# utilidades gerais
# ----------------------------------------------------------------------------
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


def achar_nome(rows, nome: str, chave: str = "nome", exato: bool = False, substring: bool = True):
    """ignora maiusculas e acentos. exato=True: so nome igual. senao aceita o comeco do nome
    (autocompletar); se mais de um nome combinar, nao escolhe nenhum."""
    alvo = norm(nome)
    for r in rows:
        if norm(r[chave]) == alvo:
            return r
    if exato:
        return None
    achados = candidatos_nome(rows, nome, chave, substring)
    return achados[0] if len(achados) == 1 else None


async def enviar_longo(ctx, texto: str):
    while texto:
        pedaco = texto[:1900]
        if len(texto) > 1900:
            corte = pedaco.rfind("\n")
            if corte > 500:
                pedaco = pedaco[:corte]
        await ctx.send(pedaco)
        texto = texto[len(pedaco):].lstrip("\n")


def eh_admin(ctx) -> bool:
    return ctx.author.guild_permissions.administrator


def pode_editar(ctx, ficha) -> bool:
    return ficha["dono_id"] == ctx.author.id or eh_admin(ctx)


async def checar_edicao(ctx, ficha) -> bool:
    if pode_editar(ctx, ficha):
        return True
    await ctx.send("🚫 voce so pode alterar a sua propria ficha.")
    return False


# ----------------------------------------------------------------------------
# fichas: acesso e calculos
# ----------------------------------------------------------------------------
def get_ficha(guild_id, nome, exato=False, substring=True):
    rows = db.execute("SELECT * FROM fichas WHERE guild_id=?", (guild_id,)).fetchall()
    return achar_nome(rows, nome, exato=exato, substring=substring)


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


def get_item(ficha_id, nome, exato=False):
    return achar_nome(db.execute("SELECT * FROM itens WHERE ficha_id=?", (ficha_id,)).fetchall(), nome, exato=exato)


def get_status(ficha_id, nome, exato=False):
    return achar_nome(db.execute("SELECT * FROM status WHERE ficha_id=?", (ficha_id,)).fetchall(), nome, exato=exato)


def get_nota(ficha_id, titulo, exato=False):
    return achar_nome(db.execute("SELECT * FROM notas WHERE ficha_id=?", (ficha_id,)).fetchall(), titulo, "titulo",
                      exato=exato)


def get_habilidade(ficha_id, nome, exato=False):
    return achar_nome(db.execute("SELECT * FROM habilidades WHERE ficha_id=?", (ficha_id,)).fetchall(), nome,
                      exato=exato)


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


def get_bonus(ficha_id) -> dict:
    """soma bonus de itens estaticos equipados (por unidade equipada) e de status estatico."""
    total = {}
    for r in db.execute("SELECT bonus, equipado FROM itens WHERE ficha_id=? AND equipado>0 AND tipo='estatico'",
                        (ficha_id,)):
        for k, v in json.loads(r["bonus"]).items():
            total[k] = total.get(k, 0) + v * r["equipado"]
    for r in db.execute("SELECT bonus FROM status WHERE ficha_id=? AND tipo='estatico'", (ficha_id,)):
        for k, v in json.loads(r["bonus"]).items():
            total[k] = total.get(k, 0) + v
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
    texto = ", ".join(f"{v:+d} {SIG[k]}" for k, v in b.items())
    return texto + (" por turno" if por_turno else "")


def parse_bonus(attrs_txt: str, vals_txt: str, permitidos):
    """'+for,-agi' e '2,1' -> {'forca': 2, 'agilidade': -1}"""
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
        try:
            valor = int(v.strip())
        except ValueError:
            return None, f"valor invalido: **{v}**."
        saida[chave] = saida.get(chave, 0) + sinal * valor
    return saida, None


def validar_nome(nome: str):
    if not nome:
        return "o nome nao pode ficar vazio."
    if len(nome) > 40:
        return "o nome deve ter ate 40 caracteres."
    return None


def embed_ficha(f) -> discord.Embed:
    bonus = get_bonus(f["id"])
    vigor, vontade = maximos(f, bonus)
    emb = discord.Embed(title=f"📜 {f['nome']}", color=discord.Color.dark_gold())
    emb.description = f"jogador: <@{f['dono_id']}> • ordem #{f['ordem']} • nivel {fmt_nivel(f['nivel'])} • xp {f['xp']}"
    linhas = []
    for k, sg in ATRIBUTOS.items():
        b = bonus.get(k, 0)
        extra = f" ({b:+d})" if b else ""
        linhas.append(f"**{sg}**: {f[k]}{extra}")
    emb.add_field(name="atributos", value="\n".join(linhas), inline=False)
    emb.add_field(name="corpo", value=f"{f['corpo']}/{vigor}", inline=True)
    emb.add_field(name="mente", value=f"{f['mente']}/{vontade}", inline=True)
    usados = sum(f[k] for k in ATRIBUTOS)
    limite_pontos = "sem limite" if f["nivel"] == -1 else str(4 * f["nivel"])
    emb.add_field(name="pontos", value=f"{usados}/{limite_pontos}", inline=True)
    eq = [f"{r['nome']} ({r['equipado']}/{r['quantidade']})" if r["quantidade"] > 1 else r["nome"]
          for r in db.execute("SELECT nome, equipado, quantidade FROM itens WHERE ficha_id=? AND equipado>0",
                              (f["id"],))]
    st = [r["nome"] for r in db.execute("SELECT nome FROM status WHERE ficha_id=?", (f["id"],))]
    emb.add_field(name="equipados", value=", ".join(eq) or "—", inline=False)
    emb.add_field(name="status", value=", ".join(st) or "—", inline=False)
    return emb


PALAVRAS_RESERVADAS = {"corpo", "mente", "crp", "mnt", "vigor", "vgr", "vontade", "vtd", "nivel", "xp",
                       "estatico", "dinamico", "consumivel", "persistente", "livre", "padrao", "reset", "sim"}


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
            await ctx.send("voce ainda nao tem ficha. use `!criarficha [nome]`.")
            return None, None
    if sem_resto and resto:
        await ctx.send(f"nao entendi **{resto[0][0]}**. use o nome da ficha ou a ordem (`#1`, `#2`...).")
        return None, None
    if editar and not await checar_edicao(ctx, ficha):
        return None, None
    return ficha, resto


# ----------------------------------------------------------------------------
# bot
# ----------------------------------------------------------------------------
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


@bot.event
async def on_ready():
    print(f"conectado como {bot.user} (id {bot.user.id})")


@bot.event
async def on_command_error(ctx, erro):
    if isinstance(erro, commands.CommandNotFound):
        return
    if isinstance(erro, commands.NoPrivateMessage):
        await ctx.send("use os comandos dentro de um servidor.")
        return
    if isinstance(erro, commands.CommandInvokeError):
        erro = erro.original
    traceback.print_exception(type(erro), erro, erro.__traceback__)
    await ctx.send("⚠️ ocorreu um erro inesperado. confira o formato do comando com `!ajuda`.")


async def confirmar(ctx, texto: str) -> bool:
    await ctx.send(f"⚠️ {texto} responda `sim` em 20 segundos.")

    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and m.content.lower().strip() in ("sim", "s")

    try:
        await bot.wait_for("message", check=check, timeout=20)
    except asyncio.TimeoutError:
        await ctx.send("cancelado.")
        return False
    return True


@bot.command(name="ajuda")
async def ajuda(ctx):
    await enviar_longo(ctx, (
        "**comandos** (nomes com espaco: use aspas; no lugar do nome pode usar a ordem da ficha, ex. `#2`; "
        "sem nada, vale a sua ficha #1; basta escrever o comeco do nome, ex. `Kage` para `KageYaeger`)\n"
        "**fichas:** `!fichas` • `!criarficha [nome] [@dono]` • `!deletarficha [nome1] [nome2] ...` • `!olharficha [nome]` • "
        "`!renomearficha [pers] [novo nome]` • `!ordem [pers] [posicao]` • `!copiarficha [pers] [novo nome] [@dono]` • `!matar [@pessoa]`\n"
        "**atributos:** `!mudar [pers] [sigla] [valor|+n|-n] [sigla2] [valor2]...` (for res agi com int sab car pre crp mnt) • "
        "`!nivel [pers] [valor|+n|-n|-1 livre]` • `!xp [pers] [valor|+n|-n]` • `!vida [pers]` • `!d20` (puro) • `!d20 [pers] [sigla] [habilidade]` • `!d20 [pers1] [pers2] [sigla]` (briga) • `!dano [rolagem] [res|sab] [defesa] [pers]` • `!iniciativa [pers1] [pers2] ...`\n"
        "**itens:** `!inventario [pers]` • `!pegar [pers] [item] [qtd] [tipo] [+sigla,-sigla] [valores]` "
        "(tipo: estatico ou consumivel, opcional) • `!descartar [pers] [item] [qtd]` • `!equipar [pers] [item]` • "
        "`!desequipar [pers] [item]` • `!equipados [pers]` • `!usaritem [pers] [item]` • "
        "`!renomearitem [pers] [item] [novo nome]` • `!limpar [pers]`\n"
        "**status:** `!addstatus [pers] [nome] [dinamico|estatico] [+sigla,-sigla] [valores] [turnos]` • "
        "`!status [pers]` • `!rmstatus [pers] [nome]` • `!renomearstatus [pers] [nome] [novo nome]` • "
        "`!turno [pers1, pers2, ...]`\n"
        "**habilidades:** `!criarhabilidade [pers] [nome] [+sigla,-sigla] [valores] [persistente|turnos]` • "
        "`!habilidades [pers]` • `!usarhabilidade [pers] [nome]` • `!desativarhabilidade [pers] [nome]` • "
        "`!apagarhabilidade [pers] [nome]` • `!renomearhabilidade [pers] [nome] [novo nome]`\n"
        "**notas:** `!anotar [pers] [nome da nota] [texto]` • `!notas [pers] [nota]` • "
        "`!editarnota [pers] [nota] [novo texto]` • `!renomearnota [pers] [nota] [novo nome]` • "
        "`!apagar [pers] [nota]` • `!queimar [pers]`\n"
        "**admin:** `!mudardono [pers] [@novo dono]` • `!limitefichas [@pessoa] [n|padrao]` • `!limitepadrao [n]`"
    ))


# ----------------------------------------------------------------------------
# fichas
# ----------------------------------------------------------------------------
@bot.command(name="fichas")
async def fichas(ctx):
    rows = db.execute(
        "SELECT nome, dono_id, nivel, ordem FROM fichas WHERE guild_id=? ORDER BY nome", (ctx.guild.id,)
    ).fetchall()
    if not rows:
        await ctx.send("nenhuma ficha criada ainda. use `!criarficha [nome]`.")
        return
    linhas = [f"• **{r['nome']}** (nv {fmt_nivel(r['nivel'])}) — <@{r['dono_id']}> #{r['ordem']}" for r in rows]
    await enviar_longo(ctx, f"📚 **{len(rows)} ficha(s) criada(s):**\n" + "\n".join(linhas))


@bot.command(name="criarficha")
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
        await ctx.send("uso: `!criarficha [nome] [@dono]` (o @dono e so para admins)")
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


@bot.command(name="deletarficha")
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
        f = get_ficha(ctx.guild.id, n)
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
    for f in alvos:
        db.execute("DELETE FROM fichas WHERE id=?", (f["id"],))
    db.commit()
    for dono in {f["dono_id"] for f in alvos}:
        renumerar(ctx.guild.id, dono)
    if len(alvos) == 1:
        await ctx.send(f"🗑️ ficha **{alvos[0]['nome']}** apagada.")
    else:
        await ctx.send(f"🗑️ {len(alvos)} fichas apagadas.")


@bot.command(name="olharficha")
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
        await ctx.send("voce ainda nao tem ficha. use `!criarficha [nome]`.")
        return
    for f in minhas:
        await ctx.send(embed=embed_ficha(f))


@bot.command(name="renomearficha")
async def renomearficha(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades=lambda n: n >= 1)
    if not f:
        return
    if not resto:
        await ctx.send("uso: `!renomearficha [pers] [novo nome]`")
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


@bot.command(name="copiarficha")
async def copiarficha(ctx, *, raw: str = ""):
    """copia uma ficha inteira (atributos, nivel, xp, corpo/mente, itens, status e notas)."""
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
    cols = ["nivel", "xp"] + list(ATRIBUTOS) + ["corpo", "mente"]
    cur = db.execute(
        f"INSERT INTO fichas (guild_id, dono_id, nome, ordem, {', '.join(cols)}) "
        f"VALUES (?, ?, ?, 999999, {', '.join('?' for _ in cols)})",
        (ctx.guild.id, dono.id, nome, *[f[c] for c in cols]))
    novo_id = cur.lastrowid
    db.execute("INSERT INTO itens (ficha_id, nome, quantidade, bonus, equipado, tipo) "
               "SELECT ?, nome, quantidade, bonus, equipado, tipo FROM itens WHERE ficha_id=?", (novo_id, f["id"]))
    db.execute("INSERT INTO status (ficha_id, nome, tipo, bonus, duracao) "
               "SELECT ?, nome, tipo, bonus, duracao FROM status WHERE ficha_id=?", (novo_id, f["id"]))
    db.execute("INSERT INTO notas (ficha_id, titulo, texto) "
               "SELECT ?, titulo, texto FROM notas WHERE ficha_id=?", (novo_id, f["id"]))
    db.execute("INSERT INTO habilidades (ficha_id, nome, bonus, duracao) "
               "SELECT ?, nome, bonus, duracao FROM habilidades WHERE ficha_id=?", (novo_id, f["id"]))
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


# ----------------------------------------------------------------------------
# atributos, nivel e xp
# ----------------------------------------------------------------------------
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
        if c in ("nivel", "xp"):
            erros.append(f"**{campo_txt}**: para nivel e experiencia use `!nivel` e `!xp`.")
            continue
        campo = achar_attr(c) or VITAIS.get(c)
        pv = parse_valor(val_txt)
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
        corpo = aplicar_valor(corpo, mud_vit["corpo"])
    if "mente" in mud_vit:
        mente = aplicar_valor(mente, mud_vit["mente"])
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


@bot.command(name="xp")
async def xp(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), numero_e_valor=True, aridades={0, 1})
    if not f:
        return
    if not resto:
        await ctx.send(f"**{f['nome']}** tem {f['xp']} de xp.")
        return
    if not await checar_edicao(ctx, f):
        return
    pv = parse_valor(resto[0][0]) if len(resto) == 1 else None
    if pv is None:
        await ctx.send("uso: `!xp [personagem] [valor ou +n/-n]`")
        return
    novo = max(0, min(aplicar_valor(f["xp"], pv), 10**9))
    db.execute("UPDATE fichas SET xp=? WHERE id=?", (novo, f["id"]))
    db.commit()
    await ctx.send(f"✨ **{f['nome']}** — xp: {f['xp']} → {novo}")


# ----------------------------------------------------------------------------
# vida e dados
# ----------------------------------------------------------------------------
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


def rolar_attr(f, attr, extra_bonus=0):
    """rola d20 + atributo, com bonus de itens/status, penalidades e o bonus do 20 natural.
    extra_bonus: bonus avulso so desta rolagem (ex.: habilidade usada no !d20)."""
    dado = random.randint(1, 20)
    base = f[attr]
    bonus = get_bonus(f["id"]).get(attr, 0)
    pen = penalidade(f["corpo"]) if f["corpo"] <= 40 else 0
    if attr in MENTAIS and f["mente"] <= 40:
        pen += penalidade(f["mente"])
    extra = int(base * 0.5) if dado == 20 else 0
    total = base + dado + bonus + extra_bonus - pen + extra
    return {"dado": dado, "base": base, "bonus": bonus, "habilidade": extra_bonus, "pen": pen,
            "extra": extra, "total": total}


def texto_rolagem(attr, r, nome_hab=None) -> str:
    partes = [f"[{r['dado']}]", f"{r['base']} ({ATRIBUTOS[attr]})"]
    if r["bonus"]:
        partes.append(f"{r['bonus']:+d} (itens/status)")
    if r.get("habilidade"):
        partes.append(f"{r['habilidade']:+d} ({nome_hab or 'habilidade'})")
    if r["pen"]:
        partes.append(f"−{r['pen']} (penalidades)")
    if r["dado"] == 20:
        partes.append(f"+{r['extra']} (20 natural)")
    return " + ".join(partes).replace("+ −", "− ").replace("+ +", "+ ")


@bot.command(name="d20")
async def d20(ctx, *, raw: str = ""):
    tokens = tokenizar(raw)
    if not tokens:  # d20 puro, sem atributo nem ficha
        dado = random.randint(1, 20)
        await ctx.send(f"🎲 d20: **{dado}**" + (" (20 natural!)" if dado == 20 else ""))
        return
    # briga de rolagem: !d20 [pers1] [pers2] ... [sigla]
    if len(tokens) >= 3 and achar_attr(tokens[-1][0]) and not achar_attr(tokens[1][0]):
        await briga_de_rolagem(ctx, tokens)
        return
    f, resto = await resolver(ctx, tokens, editar=True, aridades={1, 2})
    if not f:
        return
    attr = achar_attr(resto[0][0]) if len(resto) in (1, 2) else None
    if not attr:
        await ctx.send("uso: `!d20` (puro) • `!d20 [personagem] [sigla] [habilidade opcional]` • "
                       "briga: `!d20 [pers1] [pers2] [sigla]`  ex.: `!d20 com`, `!d20 agi cortelinear`, `!d20 douma kage pre`")
        return
    extra, nome_hab, nota = 0, None, ""
    if len(resto) == 2:
        hab = get_habilidade(f["id"], resto[1][0])
        if not hab:
            await ctx.send(msg_nao_tem(f, "habilidade", resto[1][0]))
            return
        nome_hab = hab["nome"]
        bonus_hab = json.loads(hab["bonus"]).get(attr, 0)
        if get_status(f["id"], hab["nome"], exato=True):
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
            texto += f"\n**{alvo['nome']}** — {SIG[campo]}: {antes} → {novo}"
            if novo == 0:
                texto += ("\n💀 corpo em 0: desmaiado ou morto, conforme o contexto." if campo == "corpo"
                          else "\n🌀 mente em 0.")
    elif pontos:
        texto += "\n(so calculo. para aplicar, diga o personagem no fim: `!dano 20 res 12 kage`)"
    await ctx.send(texto)


def desempatar(grupo):
    """quem empatou rola agilidade de novo, repetindo entre os que continuarem empatados."""
    if len(grupo) == 1:
        return grupo
    for it in grupo:
        it.setdefault("desempates", []).append(rolar_attr(it["f"], "agilidade")["total"])
    grupo.sort(key=lambda i: -i["desempates"][-1])
    resultado, k = [], 0
    while k < len(grupo):
        j = k
        while j < len(grupo) and grupo[j]["desempates"][-1] == grupo[k]["desempates"][-1]:
            j += 1
        resultado.extend(desempatar(grupo[k:j]))
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


# ----------------------------------------------------------------------------
# inventario
# ----------------------------------------------------------------------------
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
        if r["tipo"] == "consumivel":
            linhas.append(f"🍖 **{r['nome']}** x{r['quantidade']} [consumivel] — {bonus}")
        else:
            marca = "🟢" if r["equipado"] > 0 else "⚪"
            linhas.append(f"{marca} **{r['nome']}** x{r['quantidade']} ({r['equipado']} equipado(s)) [estatico] — {bonus}")
    await enviar_longo(ctx, f"🎒 **inventario de {f['nome']}** (🟢 equipado • ⚪ guardado • 🍖 consumivel)\n" + "\n".join(linhas))


@bot.command(name="pegar")
async def pegar(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={2, 3, 4, 5})
    if not f:
        return
    n = len(resto)
    if n not in (2, 3, 4, 5):
        await ctx.send("uso: `!pegar [personagem] [item] [quantidade] [tipo] [+sigla,-sigla] [valores]`\n"
                       "tipo (opcional): estatico ou consumivel.\n"
                       "ex.: `!pegar \"espada longa\" 1 estatico +for,-agi 3,1`\n"
                       "ex.: `!pegar pocao 3 consumivel +crp 20`")
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
            await ctx.send("o tipo precisa ser `estatico` ou `consumivel`.")
            return
    existente = get_item(f["id"], nome_item, exato=True)
    if existente and tipo and tipo != existente["tipo"]:
        await ctx.send(f"**{existente['nome']}** ja existe como {existente['tipo']}. descarte o item e crie de novo para mudar o tipo.")
        return
    tipo_final = tipo or (existente["tipo"] if existente else "estatico")
    bonus = None
    if attrs is not None:
        permitidos = set(ATRIBUTOS) if tipo_final == "estatico" else {"corpo", "mente"}
        bonus, erro = parse_bonus(attrs, vals, permitidos)
        if erro:
            if tipo_final == "consumivel":
                erro += " (consumivel afeta apenas **crp** ou **mnt**.)"
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
        msg = f"🎒 **{nome_item}** x{qtd} [{tipo_final}] ({fmt_bonus(bonus or {})}) adicionado a **{f['nome']}**."
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
    item = get_item(f["id"], resto[0][0])
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
        db.execute("UPDATE itens SET quantidade=?, equipado=? WHERE id=?",
                   (nova, min(item["equipado"], nova), item["id"]))
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
        await ctx.send(f"**{item['nome']}** e consumivel e nao se equipa. use `!usaritem`.")
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


@bot.command(name="usaritem")
async def usaritem(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!usaritem [personagem] [item]`")
        return
    item = get_item(f["id"], resto[0][0])
    if not item:
        await ctx.send(msg_nao_tem(f, "item", resto[0][0]))
        return
    if item["tipo"] != "consumivel":
        await ctx.send(f"**{item['nome']}** e estatico. use `!equipar`.")
        return
    bonus = json.loads(item["bonus"])
    vigor, vontade = maximos(f, get_bonus(f["id"]))
    valores = {"corpo": f["corpo"], "mente": f["mente"]}
    limites = {"corpo": vigor, "mente": vontade}
    linhas = []
    for k, v in bonus.items():
        antes = valores[k]
        valores[k] = max(0, min(limites[k], antes + v))
        linhas.append(f"{SIG[k]}: {antes} → {valores[k]}")
    db.execute("UPDATE fichas SET corpo=?, mente=? WHERE id=?", (valores["corpo"], valores["mente"], f["id"]))
    if item["quantidade"] > 1:
        db.execute("UPDATE itens SET quantidade=quantidade-1 WHERE id=?", (item["id"],))
        sobra = f"restam {item['quantidade'] - 1}"
    else:
        db.execute("DELETE FROM itens WHERE id=?", (item["id"],))
        sobra = "acabaram"
    db.commit()
    normalizar_vitais(f["id"])
    efeito = "\n".join(linhas) if linhas else "sem efeito mecanico."
    await ctx.send(f"🍖 **{f['nome']}** usou **{item['nome']}** ({sobra}).\n{efeito}")


# ----------------------------------------------------------------------------
# renomear (item, status, nota)
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# status
# ----------------------------------------------------------------------------
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
        dur = f"{r['duracao']} turno(s) restante(s)" if r["duracao"] else "sem duracao definida"
        linhas.append(f"🧪 **{r['nome']}** ({r['tipo']}) — {fmt_bonus(json.loads(r['bonus']), r['tipo'] == 'dinamico')} • {dur}")
    await enviar_longo(ctx, f"**status de {f['nome']}** ({len(rows)} ativo(s))\n" + "\n".join(linhas))


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
        alvo = get_status(f["id"], resto[0][0])
        if not alvo:
            await ctx.send(msg_nao_tem(f, "status", resto[0][0]))
            return
    db.execute("DELETE FROM status WHERE id=?", (alvo["id"],))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"✅ status **{alvo['nome']}** removido de **{f['nome']}**.")


# ----------------------------------------------------------------------------
# turno
# ----------------------------------------------------------------------------
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
                valores[k] = max(0, min(limites[k], valores[k] + v))
            linhas.append(f"🧪 {s['nome']}: {fmt_bonus(json.loads(s['bonus']), True)}")
        if s["duracao"] is not None:
            nova = s["duracao"] - 1
            if nova <= 0:
                db.execute("DELETE FROM status WHERE id=?", (s["id"],))
                linhas.append(f"⌛ {s['nome']} acabou.")
            else:
                db.execute("UPDATE status SET duracao=? WHERE id=?", (nova, s["id"]))
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
    blocos = [f"**⏭️ {f['nome']}**\n" + "\n".join(processar_turno(f)) for f in alvos]
    await enviar_longo(ctx, "\n\n".join(blocos))


# ----------------------------------------------------------------------------
# notas
# ----------------------------------------------------------------------------
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
    nota = get_nota(f["id"], resto[0][0])
    if not nota:
        await ctx.send(msg_nao_tem(f, "nota", resto[0][0]))
        return
    db.execute("DELETE FROM notas WHERE id=?", (nota["id"],))
    db.commit()
    await ctx.send(f"🗑️ nota **{nota['titulo']}** apagada de **{f['nome']}**.")


# ----------------------------------------------------------------------------
# ordem das fichas e limpezas em massa
# ----------------------------------------------------------------------------
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
    db.execute("DELETE FROM notas WHERE ficha_id=?", (f["id"],))
    db.commit()
    await ctx.send(f"🔥 {qtd} anotacao(oes) de **{f['nome']}** queimada(s).")


@bot.command(name="matar")
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
    nomes = ", ".join(f"**{r['nome']}**" for r in rows)
    de_quem = "suas" if alvo.id == ctx.author.id else f"de <@{alvo.id}>"
    if not await confirmar(ctx, f"apagar todas as {len(rows)} ficha(s) {de_quem} ({nomes}) com itens, status, habilidades e notas?"):
        return
    db.execute("DELETE FROM fichas WHERE guild_id=? AND dono_id=?", (ctx.guild.id, alvo.id))
    db.commit()
    await ctx.send(f"💀 {len(rows)} ficha(s) apagada(s).")


# ----------------------------------------------------------------------------
# habilidades
# ----------------------------------------------------------------------------
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


@bot.command(name="criarhabilidade")
async def criarhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={3, 4})
    if not f:
        return
    if len(resto) not in (3, 4):
        await ctx.send(
            "uso: `!criarhabilidade [personagem] [nome] [+sigla,-sigla] [valores] [persistente|turnos]`\n"
            "ex.: `!criarhabilidade Kage CorteLinear +for,+agi 2,5` (vale so no turno atual)\n"
            "ex.: `!criarhabilidade Kage ArtesMarciais +for 5 persistente`\n"
            "ex.: `!criarhabilidade Kage Furia +for 3 4` (dura 4 turnos)\n"
            "ex.: `!criarhabilidade Kage Cura +crp 15` (crp e mnt valem na hora em que a habilidade e usada)")
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
    duracao = 1
    if len(resto) == 4:
        duracao, ok = parse_duracao(resto[3][0])
        if not ok:
            await ctx.send("a duracao precisa ser `persistente` ou um numero de turnos (1, 2, 3...). "
                           "sem nada, vale so o turno atual.")
            return
    if get_habilidade(f["id"], nome, exato=True):
        await ctx.send(f"**{f['nome']}** ja tem a habilidade **{nome}**. "
                       "para trocar, apague com `!apagarhabilidade` e crie de novo.")
        return
    db.execute("INSERT INTO habilidades (ficha_id, nome, bonus, duracao) VALUES (?, ?, ?, ?)",
               (f["id"], nome, json.dumps(bonus), duracao))
    db.commit()
    if all(k in ("corpo", "mente") for k in bonus):
        dica = "use `!usarhabilidade` para aplicar o efeito."
    else:
        dica = "use `!usarhabilidade` para ativar, ou `!d20 [sigla] [habilidade]` para somar so naquela rolagem."
    await ctx.send(f"✨ habilidade **{nome}** criada em **{f['nome']}**: {fmt_bonus(bonus)} — {fmt_duracao_hab(bonus, duracao)}.\n{dica}")


@bot.command(name="habilidades")
async def habilidades(ctx, *, raw: str = ""):
    f, _ = await resolver(ctx, tokenizar(raw), sem_resto=True, aridades={0})
    if not f:
        return
    rows = db.execute("SELECT * FROM habilidades WHERE ficha_id=? ORDER BY nome", (f["id"],)).fetchall()
    if not rows:
        await ctx.send(f"**{f['nome']}** nao tem habilidades. crie com `!criarhabilidade`.")
        return
    linhas = []
    for r in rows:
        ativa = get_status(f["id"], r["nome"], exato=True)
        texto = f"{'🟢' if ativa else '⚪'} **{r['nome']}** — {fmt_bonus(json.loads(r['bonus']))} • {fmt_duracao_hab(json.loads(r['bonus']), r['duracao'])}"
        if ativa and ativa["duracao"] is not None:
            texto += f" • {ativa['duracao']} turno(s) restante(s)"
        linhas.append(texto)
    await enviar_longo(ctx, f"✨ **habilidades de {f['nome']}** (🟢 ativa • ⚪ inativa)\n" + "\n".join(linhas))


@bot.command(name="usarhabilidade")
async def usarhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!usarhabilidade [personagem] [habilidade]`")
        return
    hab = get_habilidade(f["id"], resto[0][0])
    if not hab:
        await ctx.send(msg_nao_tem(f, "habilidade", resto[0][0]))
        return
    bonus = json.loads(hab["bonus"])
    attrs = {k: v for k, v in bonus.items() if k in ATRIBUTOS}
    vitais = {k: v for k, v in bonus.items() if k in ("corpo", "mente")}
    linhas = []
    if vitais:  # crp/mnt valem na hora
        vigor, vontade = maximos(f, get_bonus(f["id"]))
        valores = {"corpo": f["corpo"], "mente": f["mente"]}
        limites = {"corpo": vigor, "mente": vontade}
        for k, v in vitais.items():
            antes = valores[k]
            valores[k] = max(0, min(limites[k], antes + v))
            linhas.append(f"{SIG[k]}: {antes} → {valores[k]}")
        db.execute("UPDATE fichas SET corpo=?, mente=? WHERE id=?", (valores["corpo"], valores["mente"], f["id"]))
    if attrs:  # bonus de atributo viram um status estatico com o nome da habilidade
        ativa = get_status(f["id"], hab["nome"], exato=True)
        if ativa and hab["duracao"] is None:
            linhas.append("ja estava ativa (persistente); nada mudou.")
        elif ativa:
            db.execute("UPDATE status SET duracao=?, bonus=? WHERE id=?",
                       (hab["duracao"], json.dumps(attrs), ativa["id"]))
            linhas.append(f"renovada: {fmt_bonus(attrs)} — {fmt_duracao(hab['duracao'])}")
        else:
            db.execute("INSERT INTO status (ficha_id, nome, tipo, bonus, duracao) VALUES (?, ?, 'estatico', ?, ?)",
                       (f["id"], hab["nome"], json.dumps(attrs), hab["duracao"]))
            linhas.append(f"ativa: {fmt_bonus(attrs)} — {fmt_duracao(hab['duracao'])}")
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"✨ **{f['nome']}** usou **{hab['nome']}**.\n" + "\n".join(linhas))


@bot.command(name="desativarhabilidade")
async def desativarhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!desativarhabilidade [personagem] [habilidade]`")
        return
    hab = get_habilidade(f["id"], resto[0][0])
    if not hab:
        await ctx.send(msg_nao_tem(f, "habilidade", resto[0][0]))
        return
    ativa = get_status(f["id"], hab["nome"], exato=True)
    if not ativa:
        await ctx.send(f"**{hab['nome']}** nao esta ativa em **{f['nome']}**.")
        return
    db.execute("DELETE FROM status WHERE id=?", (ativa["id"],))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"⏹️ **{hab['nome']}** desativada em **{f['nome']}**.")


@bot.command(name="apagarhabilidade")
async def apagarhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={1})
    if not f:
        return
    if len(resto) != 1:
        await ctx.send("uso: `!apagarhabilidade [personagem] [habilidade]`")
        return
    hab = get_habilidade(f["id"], resto[0][0])
    if not hab:
        await ctx.send(msg_nao_tem(f, "habilidade", resto[0][0]))
        return
    ativa = get_status(f["id"], hab["nome"], exato=True)
    if ativa:
        db.execute("DELETE FROM status WHERE id=?", (ativa["id"],))
    db.execute("DELETE FROM habilidades WHERE id=?", (hab["id"],))
    db.commit()
    normalizar_vitais(f["id"])
    await ctx.send(f"🗑️ habilidade **{hab['nome']}** apagada de **{f['nome']}**.")


@bot.command(name="renomearhabilidade")
async def renomearhabilidade(ctx, *, raw: str = ""):
    f, resto = await resolver(ctx, tokenizar(raw), editar=True, aridades={2})
    if not f:
        return
    if len(resto) != 2:
        await ctx.send("uso: `!renomearhabilidade [personagem] [habilidade] [novo nome]`")
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
    ativa = get_status(f["id"], hab["nome"], exato=True)
    st_novo = get_status(f["id"], novo, exato=True)
    if st_novo and (not ativa or st_novo["id"] != ativa["id"]):
        await ctx.send(f"**{f['nome']}** ja tem um status chamado **{novo}**.")
        return
    db.execute("UPDATE habilidades SET nome=? WHERE id=?", (novo, hab["id"]))
    if ativa:
        db.execute("UPDATE status SET nome=? WHERE id=?", (novo, ativa["id"]))
    db.commit()
    await ctx.send(f"✏️ habilidade **{hab['nome']}** agora se chama **{novo}**.")


# ----------------------------------------------------------------------------
# inicio
# ----------------------------------------------------------------------------
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
    bot.run(carregar_token())
