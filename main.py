
import os
import json
import asyncio
import sqlite3
import calendar
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask
from threading import Thread

# 🌷 LA GARDE — MONITORAMENTO RENDER

app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "La Garde online", 200

def iniciar_servidor():
    porta = int(os.environ.get("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=porta,
        use_reloader=False
    )

Thread(target=iniciar_servidor, daemon=True).start()


import discord
from discord import app_commands
from discord.ext import commands, tasks


# ==========================================
# 🌷 LA GARDE — CONFIGURAÇÕES
# ==========================================

DB = os.getenv("LAGARDE_DB", "lagarde.db")

TZ = ZoneInfo("Europe/Rome")

COR = 0xD8B7A0

GUILD_ID = 1561846599536083095

ENCONTRO_ID = 1561946714020126740

ENCONTRO_URL = (
    f"https://discord.com/channels/"
    f"{GUILD_ID}/{ENCONTRO_ID}"
)

ADMIN_IDS = {
    "Rubi Beaumont": 1018665053794357259,
    "Mindy": 668685948405678081,
    "Salem": 1471112680243728515,
    "Artemsi": 1455305362361028751,
}

PRECOS = {
    "Hora avulsa": 30000,
    "Meio período (4h)": 50000,
    "Diária integral (8h)": 75000,
    "Atendimento noturno": 85000,
    "Plano semanal (5 dias)": 100000,
    "Mensal — meio período": 120000,
    "Mensal — integral": 140000,
}


# ==========================================
# 🌷 FUNÇÕES AUXILIARES
# ==========================================

def agora():
    return datetime.now(TZ)


def moeda(valor):
    return "T$ " + f"{valor:,}".replace(",", ".")


def parse_data(texto):
    return datetime.strptime(
        texto.strip(),
        "%d/%m/%Y %H:%M"
    ).replace(tzinfo=TZ)


def fmt_data(data):
    return data.astimezone(TZ).strftime(
        "%d/%m/%Y %H:%M"
    )


def proximo_mes(data):
    ano = data.year
    mes = data.month + 1

    if mes == 13:
        ano += 1
        mes = 1

    dia = min(
        data.day,
        calendar.monthrange(ano, mes)[1]
    )

    return data.replace(
        year=ano,
        month=mes,
        day=dia
    )


def escapar(texto):
    return discord.utils.escape_markdown(
        str(texto or "")
    )


def status_texto(status):
    return {
        "pending": "Aguardando pagamento",
        "active": "Ativa",
        "cancel_pending": "Cancelamento programado",
        "cancelled": "Cancelada",
    }.get(status, status)


# ==========================================
# 🌷 BANCO DE DADOS
# ==========================================

def abrir_db():
    con = sqlite3.connect(
        DB,
        timeout=30
    )

    con.row_factory = sqlite3.Row

    con.execute(
        "PRAGMA foreign_keys=ON"
    )

    con.execute(
        "PRAGMA busy_timeout=30000"
    )

    return con


def inicializar_db():
    with abrir_db() as con:

        con.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id INTEGER NOT NULL UNIQUE,
                client_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                status TEXT NOT NULL,
                paid_until TEXT,
                cancel_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(booking_id)
                    REFERENCES bookings(id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id INTEGER NOT NULL,
                subscription_id INTEGER,
                cycle TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                issued_at TEXT NOT NULL,
                paid_at TEXT,
                UNIQUE(subscription_id, cycle),
                FOREIGN KEY(booking_id)
                    REFERENCES bookings(id)
            )
        """)

        # Compatibilidade com o banco antigo.
        colunas = {
            linha["name"]
            for linha in con.execute(
                "PRAGMA table_info(bookings)"
            )
        }

        if "arrival_sent" not in colunas:
            con.execute("""
                ALTER TABLE bookings
                ADD COLUMN arrival_sent
                INTEGER NOT NULL DEFAULT 0
            """)

        if "departure_sent" not in colunas:
            con.execute("""
                ALTER TABLE bookings
                ADD COLUMN departure_sent
                INTEGER NOT NULL DEFAULT 0
            """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_invoices_booking
            ON invoices(booking_id, status)
        """)


def criar_contratacao(cliente_id, dados):
    with abrir_db() as con:

        cur = con.execute(
            """
            INSERT INTO bookings
            (client_id, details, created_at)
            VALUES (?, ?, ?)
            """,
            (
                cliente_id,
                json.dumps(
                    dados,
                    ensure_ascii=False
                ),
                agora().isoformat()
            )
        )

        booking_id = cur.lastrowid
        assinatura_id = None

        if dados["plano"].startswith("Mensal"):

            cur = con.execute(
                """
                INSERT INTO subscriptions
                (
                    booking_id,
                    client_id,
                    plan,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (
                    booking_id,
                    cliente_id,
                    dados["plano"],
                    agora().isoformat()
                )
            )

            assinatura_id = cur.lastrowid

        cur = con.execute(
            """
            INSERT INTO invoices
            (
                booking_id,
                subscription_id,
                cycle,
                amount,
                status,
                issued_at
            )
            VALUES (?, ?, 'INITIAL', ?, 'pending', ?)
            """,
            (
                booking_id,
                assinatura_id,
                PRECOS[dados["plano"]],
                agora().isoformat()
            )
        )

        nota_id = cur.lastrowid

        return (
            booking_id,
            assinatura_id,
            nota_id
        )


def nota_por_id(nota_id):
    with abrir_db() as con:

        return con.execute(
            """
            SELECT
                i.*,
                b.client_id,
                b.details
            FROM invoices i
            JOIN bookings b
                ON b.id = i.booking_id
            WHERE i.id = ?
            """,
            (nota_id,)
        ).fetchone()


def assinaturas(cliente_id):
    with abrir_db() as con:

        return con.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE client_id = ?
            ORDER BY id DESC
            """,
            (cliente_id,)
        ).fetchall()


# ==========================================
# 💳 CONFIRMAÇÃO DE PAGAMENTO
# ==========================================

def confirmar_nota(nota_id):
    with abrir_db() as con:

        con.execute("BEGIN IMMEDIATE")

        nota = con.execute(
            """
            SELECT *
            FROM invoices
            WHERE id = ?
            """,
            (nota_id,)
        ).fetchone()

        if nota is None:
            return "not_found"

        if nota["status"] == "paid":
            return "already"

        if nota["status"] != "pending":
            return "invalid"

        if nota["subscription_id"]:

            assinatura = con.execute(
                """
                SELECT *
                FROM subscriptions
                WHERE id = ?
                """,
                (nota["subscription_id"],)
            ).fetchone()

            if (
                assinatura is None
                or assinatura["status"] in (
                    "cancelled",
                    "cancel_pending"
                )
            ):
                return "cancelled"

            fim_anterior = (
                datetime.fromisoformat(
                    assinatura["paid_until"]
                )
                if assinatura["paid_until"]
                else None
            )

            base = (
                max(agora(), fim_anterior)
                if fim_anterior
                else agora()
            )

            novo_fim = proximo_mes(base)

            con.execute(
                """
                UPDATE subscriptions
                SET status = 'active',
                    paid_until = ?
                WHERE id = ?
                """,
                (
                    novo_fim.isoformat(),
                    assinatura["id"]
                )
            )

        con.execute(
            """
            UPDATE invoices
            SET status = 'paid',
                paid_at = ?
            WHERE id = ?
            """,
            (
                agora().isoformat(),
                nota_id
            )
        )

        return "ok"


# ==========================================
# 💳 CANCELAMENTO DE ASSINATURA
# ==========================================

def cancelar_assinatura_db(
    assinatura_id,
    cliente_id
):
    with abrir_db() as con:

        con.execute("BEGIN IMMEDIATE")

        assinatura = con.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE id = ?
              AND client_id = ?
            """,
            (
                assinatura_id,
                cliente_id
            )
        ).fetchone()

        if assinatura is None:
            return "not_found", None

        if assinatura["status"] in (
            "cancel_pending",
            "cancelled"
        ):
            return "already", None

        fim = (
            datetime.fromisoformat(
                assinatura["paid_until"]
            )
            if assinatura["paid_until"]
            else None
        )

        status = (
            "cancel_pending"
            if fim and fim > agora()
            else "cancelled"
        )

        data_cancelamento = fim or agora()

        con.execute(
            """
            UPDATE subscriptions
            SET status = ?,
                cancel_at = ?
            WHERE id = ?
            """,
            (
                status,
                data_cancelamento.isoformat(),
                assinatura_id
            )
        )

        con.execute(
            """
            UPDATE invoices
            SET status = 'void'
            WHERE subscription_id = ?
              AND status = 'pending'
            """,
            (assinatura_id,)
        )

        return status, fim


# ==========================================
# 🌷 OS TRÊS BOTS
# ==========================================

intents = discord.Intents.default()

lagarde = commands.Bot(
    command_prefix="!",
    intents=intents
)

amelie = commands.Bot(
    command_prefix="!",
    intents=intents
)

loise = commands.Bot(
    command_prefix="!",
    intents=intents
)


@lagarde.event
async def on_ready():
    print(
        "🌷 La Garde conectada:",
        lagarde.user
    )


@amelie.event
async def on_ready():
    print(
        "🍼 Amélie conectada:",
        amelie.user
    )


@loise.event
async def on_ready():
    print(
        "🎨 Loïse conectada:",
        loise.user
    )


# ==========================================
# 🌷 NOTAS DE COBRANÇA
# ==========================================

def embed_nota(nota):
    dados = json.loads(
        nota["details"]
    )

    pago = nota["status"] == "paid"

    embed = discord.Embed(
        title=(
            "🌷 LA GARDE — PAGAMENTO CONFIRMADO"
            if pago
            else "🌷 LA GARDE — NOTA DE COBRANÇA"
        ),
        description=(
            f"**Documento LG-F{nota['id']:06d}**\n"
            f"**Referência:** {nota['cycle']}\n"
            f"**Status:** "
            f"{'Pago ✅' if pago else 'Aguardando pagamento ⏳'}"
        ),
        color=(
            0x69B48B
            if pago
            else COR
        ),
        timestamp=agora()
    )

    campos = [
        (
            "Responsável",
            dados.get("responsavel")
        ),
        (
            "Criança",
            dados.get("crianca")
        ),
        (
            "Idade",
            dados.get("idade_texto")
        ),
        (
            "Babá",
            dados.get("baba")
        ),
        (
            "Plano",
            dados.get("plano")
        ),
        (
            "Valor",
            moeda(nota["amount"])
        ),
        (
            "Entrada",
            dados.get("entrada")
        ),
        (
            "Retirada",
            dados.get("saida")
        ),
        (
            "Local de encontro",
            ENCONTRO_URL
        ),
        (
            "Alimentação",
            dados.get("alimentacao")
        ),
        (
            "Sono",
            dados.get("sono")
        ),
        (
            "Cuidados",
            dados.get("cuidados")
        ),
        (
            "Observações",
            dados.get("observacoes")
        ),
    ]

    for titulo, valor in campos:

        embed.add_field(
            name=titulo,
            value=str(
                valor or "Não informado"
            )[:230],
            inline=False
        )

    embed.set_footer(
        text=(
            "Documento fictício de RPG • "
            f"LG-F{nota['id']:06d}"
        )
    )

    return embed


# ==========================================
# 🌷 ENVIO DE MENSAGENS PRIVADAS
# ==========================================

async def enviar_dms(
    mensagem=None,
    embed=None,
    cliente_id=None,
    nota_id=None
):
    destinatarios = {
        "Cliente": cliente_id,
        **ADMIN_IDS
    }

    for nome, usuario_id in destinatarios.items():

        if not usuario_id:
            continue

        try:
            usuario = await lagarde.fetch_user(
                usuario_id
            )

            # O cliente NÃO recebe o botão.
            # Somente a administração recebe.

            view = (
                ConfirmarPagamentoView(nota_id)
                if (
                    nota_id
                    and usuario_id in ADMIN_IDS.values()
                )
                else None
            )

            await usuario.send(
                content=mensagem,
                embed=embed,
                view=view
            )

        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException
        ) as erro:

            print(
                f"⚠️ Não foi possível enviar DM "
                f"para {nome}: {erro}"
            )


async def enviar_nota(nota_id):
    nota = nota_por_id(
        nota_id
    )

    if nota:

        await enviar_dms(
            embed=embed_nota(nota),
            cliente_id=nota["client_id"],
            nota_id=(
                nota_id
                if nota["status"] == "pending"
                else None
            )
        )


# ==========================================
# 💳 BOTÃO CONFIRMAR PAGAMENTO
# ==========================================

class ConfirmarPagamentoView(discord.ui.View):

    def __init__(self, nota_id):
        super().__init__(timeout=None)

        self.nota_id = nota_id

        botao = discord.ui.Button(
            label="Confirmar pagamento",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"lagarde:pagamento:{nota_id}"
            )
        )

        botao.callback = self.ao_confirmar

        self.add_item(botao)

    async def ao_confirmar(
        self,
        interaction
    ):
        if (
            interaction.user.id
            not in ADMIN_IDS.values()
        ):

            await interaction.response.send_message(
                "❌ Somente Rubi, Mindy, Salem "
                "e Artemsi podem confirmar pagamentos.",
                ephemeral=True
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        resultado = confirmar_nota(
            self.nota_id
        )

        if resultado == "ok":

            nota = nota_por_id(
                self.nota_id
            )

            try:
                await interaction.message.edit(
                    embed=embed_nota(nota),
                    view=None
                )

            except discord.HTTPException:
                pass

            await interaction.followup.send(
                f"✅ Pagamento da nota "
                f"**LG-F{self.nota_id:06d}** "
                "confirmado!",
                ephemeral=True
            )

            await enviar_nota(
                self.nota_id
            )

        else:

            mensagens = {
                "already": (
                    "Esta nota já foi paga."
                ),
                "not_found": (
                    "Nota não encontrada."
                ),
                "invalid": (
                    "Esta nota não está pendente."
                ),
                "cancelled": (
                    "Esta assinatura está cancelada "
                    "ou em cancelamento."
                ),
            }

            if resultado == "already":

                try:
                    nota = nota_por_id(
                        self.nota_id
                    )

                    await interaction.message.edit(
                        embed=embed_nota(nota),
                        view=None
                    )

                except discord.HTTPException:
                    pass

            await interaction.followup.send(
                "⚠️ " + mensagens.get(
                    resultado,
                    "Não foi possível confirmar."
                ),
                ephemeral=True
            )


# ==========================================
# 🤍 RETIRADA AUTOMÁTICA DA CRIANÇA
# ==========================================
#
# IMPORTANTE:
#
# A babá NÃO aparece automaticamente
# no horário de entrada.
#
# O cliente chama a babá manualmente
# para entregar a criança.
#
# Somente no horário de SAÍDA
# a babá aparece automaticamente.
#
# ==========================================

async def canal_encontro(bot):

    canal = bot.get_channel(
        ENCONTRO_ID
    )

    if canal is None:

        try:
            canal = await bot.fetch_channel(
                ENCONTRO_ID
            )

        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException
        ) as erro:

            print(
                f"⚠️ {bot.user} não conseguiu "
                f"acessar o canal: {erro}"
            )

            return None

    if not hasattr(canal, "send"):

        print(
            "⚠️ O canal de encontro "
            "não permite mensagens."
        )

        return None

    return canal


def texto_retirada(dados, cliente_id):

    crianca = escapar(
        dados["crianca"]
    )

    baba = dados["baba"]

    if baba == "Amélie Laurent":

        narrativa = (
            "*Amélie Laurent retorna delicadamente "
            "ao ponto de encontro, acompanhando "
            "a criança com carinho e atenção. "
            "Com um sorriso acolhedor, aguarda "
            "a chegada do responsável.*\n\n"
            f"— Bonjour! Nosso momento com "
            f"**{crianca}** chegou ao fim. "
            "Foi um prazer cuidar deste "
            "pequeno tesouro! 🌷"
        )

    else:

        narrativa = (
            "*Loïse Moreau retorna à recepção "
            "da La Garde, acompanhando a criança "
            "após um dia de atividades e descobertas. "
            "Ela sorri enquanto aguarda "
            "o responsável.*\n\n"
            f"— Bonjour! **{crianca}** teve "
            "um dia maravilhoso conosco! "
            "Chegou a hora de voltar para casa. "
            "Até nossa próxima aventura! 🎨"
        )

    mensagem = (
        "🤍 **LA GARDE — HORÁRIO DE RETIRADA**\n\n"
        f"**Responsável:** <@{cliente_id}>\n"
        f"**Criança:** {crianca}\n"
        f"**Babá:** {baba}\n"
        f"**Horário de retirada:** "
        f"{dados['saida']}\n\n"
        f"{narrativa}"
    )

    return mensagem


# ==========================================
# 🤍 VERIFICAR HORÁRIOS DE SAÍDA
# ==========================================

async def processar_encontros():

    atual = agora()

    with abrir_db() as con:

        registros = con.execute(
            """
            SELECT b.*
            FROM bookings b
            WHERE EXISTS (
                SELECT 1
                FROM invoices i
                WHERE i.booking_id = b.id
                  AND i.status = 'paid'
            )
            AND b.departure_sent = 0
            ORDER BY b.id
            """
        ).fetchall()

    for registro in registros:

        try:
            dados = json.loads(
                registro["details"]
            )

            saida = parse_data(
                dados["saida"]
            )

            # Não faz nada antes da saída.
            if atual < saida:
                continue

            # Escolhe a babá correspondente.
            if (
                dados.get("baba")
                == "Amélie Laurent"
            ):

                bot = amelie

            else:

                bot = loise

            if not bot.is_ready():
                continue

            canal = await canal_encontro(
                bot
            )

            if canal is None:
                continue

            # A babá aparece SOMENTE
            # no horário de retirada.

            await canal.send(
                texto_retirada(
                    dados,
                    registro["client_id"]
                ),
                allowed_mentions=(
                    discord.AllowedMentions(
                        users=True
                    )
                )
            )

            # Registra que a retirada
            # já foi anunciada.

            with abrir_db() as con:

                con.execute(
                    """
                    UPDATE bookings
                    SET departure_sent = 1
                    WHERE id = ?
                    """,
                    (registro["id"],)
                )

            print(
                "🤍 Retirada automática enviada: "
                f"LG-{registro['id']:06d}"
            )

        except Exception as erro:

            print(
                "⚠️ Erro na retirada "
                f"LG-{registro['id']:06d}: "
                f"{erro!r}"
            )


# ==========================================
# 🤍 ROTINA DE RETIRADA
# ==========================================

@tasks.loop(seconds=30)
async def rotina_encontros():

    try:
        await processar_encontros()

    except Exception as erro:

        print(
            "⚠️ Erro na rotina de retirada:",
            repr(erro)
        )


@rotina_encontros.before_loop
async def aguardar_babas():

    await asyncio.gather(
        lagarde.wait_until_ready(),
        amelie.wait_until_ready(),
        loise.wait_until_ready()
    )


# ==========================================
# 💳 COBRANÇAS MENSAIS — DIA 5
# ==========================================

async def processar_mensalidades():

    atual = agora()
    novas_notas = []

    with abrir_db() as con:

        registros = con.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE status IN (
                'active',
                'cancel_pending'
            )
            """
        ).fetchall()

        for assinatura in registros:

            if (
                assinatura["status"]
                == "cancel_pending"
            ):

                if (
                    assinatura["cancel_at"]
                    and datetime.fromisoformat(
                        assinatura["cancel_at"]
                    ) <= atual
                ):

                    con.execute(
                        """
                        UPDATE subscriptions
                        SET status = 'cancelled'
                        WHERE id = ?
                        """,
                        (assinatura["id"],)
                    )

                continue

            if atual.day < 5:
                continue

            ciclo = atual.strftime(
                "%Y-%m"
            )

            existente = con.execute(
                """
                SELECT 1
                FROM invoices
                WHERE subscription_id = ?
                  AND cycle = ?
                """,
                (
                    assinatura["id"],
                    ciclo
                )
            ).fetchone()

            pendente = con.execute(
                """
                SELECT 1
                FROM invoices
                WHERE subscription_id = ?
                  AND status = 'pending'
                """,
                (assinatura["id"],)
            ).fetchone()

            if existente or pendente:
                continue

            fim = (
                datetime.fromisoformat(
                    assinatura["paid_until"]
                )
                if assinatura["paid_until"]
                else None
            )

            vencimento = atual.replace(
                day=5,
                hour=0,
                minute=0,
                second=0,
                microsecond=0
            )

            if (
                fim
                and fim > proximo_mes(vencimento)
            ):
                continue

            cur = con.execute(
                """
                INSERT INTO invoices
                (
                    booking_id,
                    subscription_id,
                    cycle,
                    amount,
                    status,
                    issued_at
                )
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    assinatura["booking_id"],
                    assinatura["id"],
                    ciclo,
                    PRECOS[assinatura["plan"]],
                    atual.isoformat()
                )
            )

            novas_notas.append(
                cur.lastrowid
            )

    for nota_id in novas_notas:

        await enviar_nota(
            nota_id
        )


@tasks.loop(minutes=15)
async def rotina_cobrancas():

    try:
        await processar_mensalidades()

    except Exception as erro:

        print(
            "⚠️ Erro nas cobranças:",
            repr(erro)
        )


@rotina_cobrancas.before_loop
async def aguardar_lagarde():

    await lagarde.wait_until_ready()


# ==========================================
# 🌷 INICIALIZAÇÃO DOS COMANDOS
# ==========================================

@lagarde.event
async def setup_hook():

    # Restaura botões das notas pendentes.

    with abrir_db() as con:

        notas = con.execute(
            """
            SELECT id
            FROM invoices
            WHERE status = 'pending'
            """
        ).fetchall()

    for nota in notas:

        lagarde.add_view(
            ConfirmarPagamentoView(
                nota["id"]
            )
        )

    comandos = await lagarde.tree.sync()

    if not rotina_cobrancas.is_running():
        rotina_cobrancas.start()

    if not rotina_encontros.is_running():
        rotina_encontros.start()

    print(
        f"🌷 {len(comandos)} comandos sincronizados!"
    )


# ==========================================
# 🌷 SEGURANÇA DOS FORMULÁRIOS
# ==========================================

async def conferir_dono(
    interaction,
    cliente_id
):

    if interaction.user.id != cliente_id:

        await interaction.response.send_message(
            "Este formulário pertence "
            "a outro usuário.",
            ephemeral=True
        )

        return False

    return True


# ==========================================
# 🌷 SELEÇÃO DE PLANO
# ==========================================

class PlanoSelect(discord.ui.Select):

    def __init__(self, cliente_id):

        self.cliente_id = cliente_id

        opcoes = [
            discord.SelectOption(
                label=nome,
                description=moeda(valor)
            )
            for nome, valor in PRECOS.items()
        ]

        super().__init__(
            placeholder="Escolha seu plano",
            options=opcoes
        )

    async def callback(
        self,
        interaction
    ):

        if not await conferir_dono(
            interaction,
            self.cliente_id
        ):
            return

        await interaction.response.send_message(
            "🍼 **Selecione a idade da criança:**",
            view=IdadeView(
                {
                    "plano": self.values[0]
                },
                self.cliente_id
            ),
            ephemeral=True
        )


class PlanoView(discord.ui.View):

    def __init__(self, cliente_id):

        super().__init__(timeout=900)

        self.add_item(
            PlanoSelect(
                cliente_id
            )
        )


# ==========================================
# 🌷 IDADE DA CRIANÇA
# ==========================================

class IdadeSelect(discord.ui.Select):

    def __init__(
        self,
        dados,
        cliente_id
    ):

        self.dados = dados
        self.cliente_id = cliente_id

        opcoes = []

        for idade in range(14):

            if idade == 0:
                texto = "Menos de 1 ano"

            elif idade == 1:
                texto = "1 ano"

            else:
                texto = f"{idade} anos"

            opcoes.append(
                discord.SelectOption(
                    label=texto,
                    value=str(idade)
                )
            )

        super().__init__(
            placeholder="Idade da criança",
            options=opcoes
        )

    async def callback(
        self,
        interaction
    ):

        if not await conferir_dono(
            interaction,
            self.cliente_id
        ):
            return

        idade = int(
            self.values[0]
        )

        idade_texto = (
            "Menos de 1 ano"
            if idade == 0
            else f"{idade} ano"
            if idade == 1
            else f"{idade} anos"
        )

        baba = (
            "Amélie Laurent"
            if idade <= 4
            else "Loïse Moreau"
        )

        self.dados.update({
            "idade": idade,
            "idade_texto": idade_texto,
            "baba": baba
        })

        await interaction.response.send_message(
            f"🌷 **Babá responsável:** {baba}\n\n"
            "Agora informe os dados e horários.",
            view=DadosView(
                self.dados,
                self.cliente_id
            ),
            ephemeral=True
        )


class IdadeView(discord.ui.View):

    def __init__(
        self,
        dados,
        cliente_id
    ):

        super().__init__(timeout=900)

        self.add_item(
            IdadeSelect(
                dados,
                cliente_id
            )
        )


# ==========================================
# 🌷 DADOS E HORÁRIOS
# ==========================================

class DadosModal(
    discord.ui.Modal,
    title="La Garde | Datas e horários"
):

    responsavel = discord.ui.TextInput(
        label="Nome do responsável",
        max_length=100
    )

    crianca = discord.ui.TextInput(
        label="Nome da criança",
        max_length=100
    )

    entrada = discord.ui.TextInput(
        label="Data e hora de entrada",
        placeholder="20/11/2026 09:00",
        max_length=16
    )

    saida = discord.ui.TextInput(
        label="Data e hora de retirada",
        placeholder="20/11/2026 13:00",
        max_length=16
    )

    def __init__(
        self,
        dados,
        cliente_id
    ):

        super().__init__()

        self.dados = dados
        self.cliente_id = cliente_id

    async def on_submit(
        self,
        interaction
    ):

        if not await conferir_dono(
            interaction,
            self.cliente_id
        ):
            return

        try:

            inicio = parse_data(
                self.entrada.value
            )

            fim = parse_data(
                self.saida.value
            )

            if (
                inicio <= agora()
                or fim <= inicio
            ):
                raise ValueError()

        except ValueError:

            await interaction.response.send_message(
                "❌ Datas inválidas.\n\n"
                "Use **DD/MM/AAAA HH:MM**, "
                "no horário da Itália.\n"
                "A entrada deve ser futura "
                "e a retirada posterior.",
                ephemeral=True
            )

            return

        self.dados.update({
            "responsavel": (
                self.responsavel.value.strip()
            ),
            "crianca": (
                self.crianca.value.strip()
            ),
            "entrada": fmt_data(inicio),
            "saida": fmt_data(fim)
        })

        await interaction.response.send_message(
            "🌷 **Local oficial de atendimento:**\n"
            f"<#{ENCONTRO_ID}>\n"
            f"{ENCONTRO_URL}\n\n"
            "**Na entrada:** chame sua babá "
            "manualmente neste canal.\n\n"
            "**Na saída:** a babá retornará "
            "automaticamente no horário marcado.\n\n"
            "Agora informe os cuidados da criança.",
            view=CuidadosView(
                self.dados,
                self.cliente_id
            ),
            ephemeral=True
        )


class DadosView(discord.ui.View):

    def __init__(
        self,
        dados,
        cliente_id
    ):

        super().__init__(timeout=900)

        self.dados = dados
        self.cliente_id = cliente_id

    @discord.ui.button(
        label="Informar datas e horários",
        emoji="📅",
        style=discord.ButtonStyle.primary
    )
    async def abrir(
        self,
        interaction,
        button
    ):

        if await conferir_dono(
            interaction,
            self.cliente_id
        ):

            await interaction.response.send_modal(
                DadosModal(
                    self.dados,
                    self.cliente_id
                )
            )


# ==========================================
# 🌷 CUIDADOS INFANTIS
# ==========================================

class CuidadosModal(
    discord.ui.Modal,
    title="La Garde | Cuidados infantis"
):

    alimentacao = discord.ui.TextInput(
        label="Alimentação",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=400
    )

    sono = discord.ui.TextInput(
        label="Rotina de sono",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=400
    )

    cuidados = discord.ui.TextInput(
        label="Cuidados especiais",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=400
    )

    observacoes = discord.ui.TextInput(
        label="Observações",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=400
    )

    def __init__(
        self,
        dados,
        cliente_id
    ):

        super().__init__()

        self.dados = dados
        self.cliente_id = cliente_id

    async def on_submit(
        self,
        interaction
    ):

        if not await conferir_dono(
            interaction,
            self.cliente_id
        ):
            return

        self.dados.update({
            "alimentacao": self.alimentacao.value,
            "sono": self.sono.value,
            "cuidados": self.cuidados.value,
            "observacoes": self.observacoes.value
        })

        dados = self.dados

        descricao = (
            f"**Responsável:** "
            f"{escapar(dados['responsavel'])}\n"
            f"**Criança:** "
            f"{escapar(dados['crianca'])}\n"
            f"**Idade:** {dados['idade_texto']}\n"
            f"**Babá:** {dados['baba']}\n\n"
            f"**Plano:** {dados['plano']}\n"
            f"**Valor:** "
            f"{moeda(PRECOS[dados['plano']])}\n\n"
            f"**Entrada:** {dados['entrada']}\n"
            f"**Retirada:** {dados['saida']}\n"
            f"**Local:** {ENCONTRO_URL}\n\n"
            "**Entrada:** Cliente chama a babá.\n"
            "**Saída:** Babá retorna automaticamente.\n\n"
            "**Pagamento:** Aguardando pagamento"
        )

        if dados["plano"].startswith("Mensal"):

            descricao += (
                "\n\n**Assinatura mensal:** "
                "cobranças no dia 5 e "
                "cancelamento sem novas cobranças."
            )

        embed = discord.Embed(
            title="🌷 Confira sua contratação",
            description=descricao,
            color=COR
        )

        await interaction.response.send_message(
            embed=embed,
            view=ConfirmarContratacaoView(
                self.dados,
                self.cliente_id
            ),
            ephemeral=True
        )


class CuidadosView(discord.ui.View):

    def __init__(
        self,
        dados,
        cliente_id
    ):

        super().__init__(timeout=900)

        self.dados = dados
        self.cliente_id = cliente_id

    @discord.ui.button(
        label="Informar cuidados",
        emoji="🍼",
        style=discord.ButtonStyle.primary
    )
    async def abrir(
        self,
        interaction,
        button
    ):

        if await conferir_dono(
            interaction,
            self.cliente_id
        ):

            await interaction.response.send_modal(
                CuidadosModal(
                    self.dados,
                    self.cliente_id
                )
            )


# ==========================================
# 🌷 CONFIRMAR CONTRATAÇÃO
# ==========================================

class ConfirmarContratacaoView(discord.ui.View):

    def __init__(
        self,
        dados,
        cliente_id
    ):

        super().__init__(timeout=900)

        self.dados = dados
        self.cliente_id = cliente_id
        self.processando = False

    @discord.ui.button(
        label="Confirmar solicitação",
        emoji="🌷",
        style=discord.ButtonStyle.success
    )
    async def confirmar(
        self,
        interaction,
        button
    ):

        if not await conferir_dono(
            interaction,
            self.cliente_id
        ):
            return

        if self.processando:

            await interaction.response.send_message(
                "Esta solicitação já está "
                "sendo processada.",
                ephemeral=True
            )
            return

        self.processando = True

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            (
                booking_id,
                assinatura_id,
                nota_id
            ) = criar_contratacao(
                self.cliente_id,
                self.dados
            )

            await interaction.followup.send(
                f"🌷 **Solicitação "
                f"LG-{booking_id:06d} registrada!**\n\n"
                f"**Nota:** LG-F{nota_id:06d}\n"
                f"**Babá:** {self.dados['baba']}\n"
                f"**Entrada:** {self.dados['entrada']}\n"
                f"**Retirada:** {self.dados['saida']}\n"
                f"**Local:** {ENCONTRO_URL}\n\n"
                "**Status:** Aguardando pagamento.\n\n"
                "Após a confirmação, você poderá "
                "chamar a babá no horário de entrada.\n"
                "Ela retornará automaticamente "
                "no horário de retirada.",
                ephemeral=True
            )

            await enviar_nota(
                nota_id
            )

        except Exception as erro:

            print(
                "❌ Erro na contratação:",
                repr(erro)
            )

            await interaction.followup.send(
                "⚠️ Erro ao registrar. "
                "Consulte a administração "
                "antes de tentar novamente.",
                ephemeral=True
            )

    @discord.ui.button(
        label="Cancelar formulário",
        emoji="✖️",
        style=discord.ButtonStyle.secondary
    )
    async def cancelar(
        self,
        interaction,
        button
    ):

        if not await conferir_dono(
            interaction,
            self.cliente_id
        ):
            return

        if self.processando:

            await interaction.response.send_message(
                "A solicitação já está "
                "sendo processada.",
                ephemeral=True
            )
            return

        self.stop()

        await interaction.response.edit_message(
            content="🌷 Formulário cancelado.",
            embed=None,
            view=None
        )


# ==========================================
# 💳 MINHA ASSINATURA
# ==========================================

async def mostrar_assinaturas(
    interaction
):

    registros = assinaturas(
        interaction.user.id
    )

    if not registros:

        await interaction.response.send_message(
            "🌷 Você ainda não possui "
            "assinatura mensal.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="💳 Minha Assinatura — La Garde",
        color=COR
    )

    for assinatura in registros[:10]:

        fim = (
            datetime.fromisoformat(
                assinatura["paid_until"]
            )
            if assinatura["paid_until"]
            else None
        )

        status = status_texto(
            assinatura["status"]
        )

        proxima_cobranca = (
            "Nenhuma"
            if assinatura["status"] in (
                "cancel_pending",
                "cancelled"
            )
            else "Dia 5"
        )

        embed.add_field(
            name=(
                f"🌷 Assinatura "
                f"#{assinatura['id']}"
            ),
            value=(
                f"**Plano:** {assinatura['plan']}\n"
                f"**Valor:** "
                f"{moeda(PRECOS[assinatura['plan']])}\n"
                f"**Status:** {status}\n"
                f"**Pago até:** "
                f"{fmt_data(fim) if fim else 'Aguardando pagamento'}\n"
                f"**Próxima cobrança:** "
                f"{proxima_cobranca}"
            ),
            inline=False
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ==========================================
# 💳 CONFIRMAR CANCELAMENTO
# ==========================================

class ConfirmarCancelamentoView(discord.ui.View):

    def __init__(
        self,
        assinatura_id,
        cliente_id
    ):

        super().__init__(timeout=300)

        self.assinatura_id = assinatura_id
        self.cliente_id = cliente_id
        self.processando = False

    @discord.ui.button(
        label="Confirmar cancelamento",
        emoji="🛑",
        style=discord.ButtonStyle.danger
    )
    async def confirmar(
        self,
        interaction,
        button
    ):

        if not await conferir_dono(
            interaction,
            self.cliente_id
        ):
            return

        if self.processando:

            await interaction.response.send_message(
                "Cancelamento já processado.",
                ephemeral=True
            )
            return

        self.processando = True

        status, fim = cancelar_assinatura_db(
            self.assinatura_id,
            self.cliente_id
        )

        if status in (
            "not_found",
            "already"
        ):

            await interaction.response.send_message(
                "Assinatura não encontrada "
                "ou já cancelada.",
                ephemeral=True
            )
            return

        if status == "cancel_pending":

            texto = (
                "🌷 **Cancelamento programado.**\n"
                "Atendimento até "
                f"**{fmt_data(fim)}**."
            )

        else:

            texto = (
                "🌷 **Assinatura cancelada.**"
            )

        texto += (
            "\n\n**Nenhuma nova cobrança "
            "será emitida.**"
        )

        await interaction.response.send_message(
            texto,
            ephemeral=True
        )

        await enviar_dms(
            mensagem=(
                "🌷 **La Garde — "
                f"Assinatura #{self.assinatura_id}**\n\n"
                f"{texto}"
            ),
            cliente_id=self.cliente_id
        )

    @discord.ui.button(
        label="Voltar",
        emoji="↩️",
        style=discord.ButtonStyle.secondary
    )
    async def voltar(
        self,
        interaction,
        button
    ):

        if await conferir_dono(
            interaction,
            self.cliente_id
        ):

            await interaction.response.edit_message(
                content="Cancelamento não realizado.",
                view=None
            )


# ==========================================
# 💳 SELECIONAR ASSINATURA
# ==========================================

class CancelamentoSelect(discord.ui.Select):

    def __init__(
        self,
        registros,
        cliente_id
    ):

        self.cliente_id = cliente_id

        opcoes = [
            discord.SelectOption(
                label=(
                    f"#{assinatura['id']} — "
                    f"{assinatura['plan']}"
                )[:100],
                value=str(
                    assinatura["id"]
                )
            )
            for assinatura in registros[:25]
        ]

        super().__init__(
            placeholder="Escolha a assinatura",
            options=opcoes
        )

    async def callback(
        self,
        interaction
    ):

        if not await conferir_dono(
            interaction,
            self.cliente_id
        ):
            return

        assinatura_id = int(
            self.values[0]
        )

        registros = [
            assinatura
            for assinatura in assinaturas(
                self.cliente_id
            )
            if (
                assinatura["id"] == assinatura_id
                and assinatura["status"] in (
                    "pending",
                    "active"
                )
            )
        ]

        if not registros:

            await interaction.response.send_message(
                "Assinatura indisponível.",
                ephemeral=True
            )
            return

        assinatura = registros[0]

        fim = (
            datetime.fromisoformat(
                assinatura["paid_until"]
            )
            if assinatura["paid_until"]
            else None
        )

        mensagem = (
            f"🌷 **Cancelar assinatura "
            f"#{assinatura_id}?**\n\n"
        )

        if fim and fim > agora():

            mensagem += (
                "O atendimento permanece até "
                f"**{fmt_data(fim)}**.\n\n"
            )

        mensagem += (
            "**Você não receberá "
            "a próxima cobrança.**"
        )

        await interaction.response.send_message(
            mensagem,
            view=ConfirmarCancelamentoView(
                assinatura_id,
                self.cliente_id
            ),
            ephemeral=True
        )


class CancelamentoSelectView(discord.ui.View):

    def __init__(
        self,
        registros,
        cliente_id
    ):

        super().__init__(timeout=300)

        self.add_item(
            CancelamentoSelect(
                registros,
                cliente_id
            )
        )


async def mostrar_cancelamento(
    interaction
):

    registros = [
        assinatura
        for assinatura in assinaturas(
            interaction.user.id
        )
        if assinatura["status"] in (
            "pending",
            "active"
        )
    ]

    if not registros:

        await interaction.response.send_message(
            "🌷 Você não possui assinatura "
            "disponível para cancelamento.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "🌷 **Selecione a assinatura "
        "que deseja cancelar:**",
        view=CancelamentoSelectView(
            registros,
            interaction.user.id
        ),
        ephemeral=True
    )


# ==========================================
# 🌷 MENU PRINCIPAL
# ==========================================

class MenuLaGarde(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(
        label="Catálogo & Valores",
        emoji="📖",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def catalogo(
        self,
        interaction,
        button
    ):

        descricao = "\n".join(
            f"**{nome}:** {moeda(valor)}"
            for nome, valor in PRECOS.items()
        )

        embed = discord.Embed(
            title="📖 Catálogo & Valores",
            description=descricao,
            color=COR
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    @discord.ui.button(
        label="Experiência La Garde",
        emoji="🌷",
        style=discord.ButtonStyle.secondary,
        row=0
    )
    async def experiencia(
        self,
        interaction,
        button
    ):

        await interaction.response.send_message(
            "🍼 **Amélie Laurent:** "
            "0 a 4 anos — primeiros cuidados, "
            "rotinas e estímulos sensoriais.\n\n"
            "🎨 **Loïse Moreau:** "
            "5 a 13 anos — atividades educativas, "
            "brincadeiras e autonomia.\n\n"
            f"📍 **Local:** {ENCONTRO_URL}\n\n"
            "**Entrada:** O cliente chama a babá.\n"
            "**Saída:** A babá aparece automaticamente.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Solicitar Acompanhamento",
        emoji="🍼",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def solicitar(
        self,
        interaction,
        button
    ):

        if not interaction.guild:

            await interaction.response.send_message(
                "Use este aplicativo "
                "dentro do servidor.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🌷 **Escolha seu plano:**",
            view=PlanoView(
                interaction.user.id
            ),
            ephemeral=True
        )

    @discord.ui.button(
        label="Minha Assinatura",
        emoji="💳",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def assinatura(
        self,
        interaction,
        button
    ):

        await mostrar_assinaturas(
            interaction
        )

    @discord.ui.button(
        label="Cancelar Assinatura",
        emoji="✖️",
        style=discord.ButtonStyle.danger,
        row=1
    )
    async def cancelar(
        self,
        interaction,
        button
    ):

        await mostrar_cancelamento(
            interaction
        )


# ==========================================
# 🌷 COMANDO /LAGARDE
# ==========================================

@lagarde.tree.command(
    name="lagarde",
    description="Abrir o aplicativo oficial da La Garde"
)
async def comando_lagarde(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="🌷 LA GARDE — ITÁLIA 🇮🇹",
        description=(
            "*Un luogo di cura, "
            "presenza e continuità.*\n\n"
            "Bem-vindo à La Garde!\n\n"
            "Selecione uma opção abaixo."
        ),
        color=COR
    )

    await interaction.response.send_message(
        embed=embed,
        view=MenuLaGarde(),
        ephemeral=True
    )


# ==========================================
# 💳 COMANDO MINHA ASSINATURA
# ==========================================

@lagarde.tree.command(
    name="minha_assinatura",
    description="Consultar suas assinaturas mensais"
)
async def comando_assinatura(
    interaction: discord.Interaction
):

    await mostrar_assinaturas(
        interaction
    )


# ==========================================
# 💳 COMANDO CANCELAR ASSINATURA
# ==========================================

@lagarde.tree.command(
    name="cancelar_lagarde",
    description="Cancelar uma assinatura mensal"
)
async def comando_cancelar(
    interaction: discord.Interaction
):

    await mostrar_cancelamento(
        interaction
    )


# ==========================================
# 💳 COMANDO CONFIRMAR PAGAMENTO
# ==========================================

@lagarde.tree.command(
    name="confirmar_pagamento",
    description="ADMIN: confirmar pagamento de uma nota"
)
@app_commands.describe(
    nota="Número da nota: 12 para LG-F000012"
)
async def comando_pagamento(
    interaction: discord.Interaction,
    nota: int
):

    if (
        interaction.user.id
        not in ADMIN_IDS.values()
    ):

        await interaction.response.send_message(
            "❌ Comando exclusivo "
            "da administração.",
            ephemeral=True
        )
        return

    if nota <= 0:

        await interaction.response.send_message(
            "❌ Número inválido.",
            ephemeral=True
        )
        return

    await interaction.response.defer(
        ephemeral=True
    )

    resultado = confirmar_nota(
        nota
    )

    if resultado == "ok":

        await interaction.followup.send(
            f"✅ Pagamento da nota "
            f"**LG-F{nota:06d}** confirmado!",
            ephemeral=True
        )

        await enviar_nota(
            nota
        )

    else:

        mensagens = {
            "already": "Nota já paga.",
            "not_found": "Nota não encontrada.",
            "invalid": "Nota não está pendente.",
            "cancelled": "Assinatura cancelada.",
        }

        await interaction.followup.send(
            "⚠️ " + mensagens.get(
                resultado,
                "Não foi possível confirmar."
            ),
            ephemeral=True
        )


# ==========================================
# 🌷 INICIAR OS TRÊS BOTS
# ==========================================

async def iniciar():

    inicializar_db()

    token_lagarde = os.getenv(
        "DISCORD_TOKEN"
    )

    token_amelie = os.getenv(
        "Amelie"
    )

    token_loise = os.getenv(
        "Loise"
    )

    if not all((
        token_lagarde,
        token_amelie,
        token_loise
    )):

        raise RuntimeError(
            "Confira os Secrets do Replit:\n"
            "DISCORD_TOKEN\n"
            "Amelie\n"
            "Loise"
        )

    print(
        "🌷 Iniciando os três bots da La Garde..."
    )

    async with lagarde, amelie, loise:

        await asyncio.gather(
            lagarde.start(
                token_lagarde
            ),
            amelie.start(
                token_amelie
            ),
            loise.start(
                token_loise
            )
        )


if __name__ == "__main__":

    try:
        asyncio.run(
            iniciar()
        )

    except KeyboardInterrupt:

        print(
            "🌷 La Garde encerrada."
        )
