import html
import json
import os
import re
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from anthropic import Anthropic

st.set_page_config(page_title="Radar SECOP II", page_icon="⚖️", layout="wide")

MODELO = "claude-sonnet-5-5"
SECOP_URL = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
ALERTAS_FILE = Path("alertas_enviadas.json")

# ---------------- Estilos ----------------
st.markdown(
    """
<style>
.stApp {background: linear-gradient(135deg,#eef2ff 0%,#f0f9ff 45%,#ecfdf5 100%);}
[data-testid="stSidebar"] {background: rgba(255,255,255,.92);}
div[data-testid="stVerticalBlockBorderWrapper"] {background:#fff;border-radius:16px;
  box-shadow:0 6px 20px rgba(15,23,42,.07);}
.block-container {padding-top:2rem;max-width:1250px;}
.hero {padding:1.3rem 1.6rem;border-radius:18px;background:linear-gradient(120deg,#4f46e5,#06b6d4);
  margin-bottom:1rem;box-shadow:0 10px 25px rgba(79,70,229,.25);}
.hero h1 {color:#fff !important;margin:0;font-size:1.7rem;}
.hero p {color:#e0f2fe !important;margin:.25rem 0 0 0;}
.stats {display:flex;gap:.75rem;flex-wrap:wrap;margin:.5rem 0 1rem 0;}
.stat {flex:1;min-width:120px;background:#fff;border-radius:14px;padding:.8rem 1rem;
  box-shadow:0 4px 14px rgba(0,0,0,.07);}
.stat b {display:block;font-size:1.6rem;color:#111827;}
.stat span {color:#6b7280;font-size:.85rem;}
.grid {display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:1rem;margin-bottom:1rem;}
.card {background:#fff;border-radius:16px;padding:1.1rem 1.3rem;
  box-shadow:0 6px 20px rgba(15,23,42,.08);border-left:8px solid var(--c);}
.card-head {display:flex;justify-content:space-between;align-items:flex-start;gap:.8rem;}
.card h3 {margin:0;font-size:1rem;color:#111827;line-height:1.3;}
.badge {white-space:nowrap;padding:.25rem .7rem;border-radius:999px;font-weight:600;
  font-size:.78rem;background:var(--c);color:#fff;}
.price {font-size:1.3rem;font-weight:700;color:#111827;margin:.45rem 0;}
.meta {color:#6b7280;font-size:.8rem;}
.card p {margin:.35rem 0;color:#374151;font-size:.92rem;}
.card p.obj {display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;}
.card details {margin-top:.4rem;color:#374151;font-size:.9rem;}
.card summary {cursor:pointer;color:#4f46e5;font-weight:600;}
.card a.btn {display:inline-block;margin-top:.6rem;padding:.45rem .95rem;border-radius:10px;
  background:#4f46e5;color:#fff !important;text-decoration:none;font-weight:600;font-size:.85rem;}
.welcome {background:#fff;border-radius:16px;padding:1.4rem 1.6rem;box-shadow:0 6px 20px rgba(15,23,42,.07);color:#374151;}
.welcome h3 {margin-top:0;color:#111827;}
</style>
""",
    unsafe_allow_html=True,
)

# Cada región es un fragmento de filtro SoQL (sin depender de tildes)
REGIONES = {
    "Nariño (todo el departamento)": "upper(departamento_entidad) like 'NARI%'",
    "Nariño · Pasto": "upper(departamento_entidad) like 'NARI%' AND upper(ciudad_entidad) like '%PASTO%'",
    "Nariño · Ipiales": "upper(departamento_entidad) like 'NARI%' AND upper(ciudad_entidad) like '%IPIALES%'",
    "Nariño · Túquerres": "upper(departamento_entidad) like 'NARI%' AND upper(ciudad_entidad) like '%QUERRES%'",
    "Cauca": "upper(departamento_entidad)='CAUCA'",
    "Putumayo": "upper(departamento_entidad)='PUTUMAYO'",
    "Valle del Cauca": "upper(departamento_entidad) like 'VALLE%'",
    "Huila": "upper(departamento_entidad)='HUILA'",
    "Bogotá D.C.": "upper(departamento_entidad) like '%BOGOT%'",
    "Cundinamarca": "upper(departamento_entidad)='CUNDINAMARCA'",
    "Antioquia": "upper(departamento_entidad)='ANTIOQUIA'",
}

PERFIL = (
    "Abogado en ejercicio (persona natural), con un presupuesto de operación "
    "de 10 a 20 millones de COP, sin experiencia formal en RUP "
    "(Registro Único de Proponentes)."
)

SYSTEM_RAPIDO = f"""Eres un asesor experto en contratación estatal colombiana
y en procesos de Mínima Cuantía en SECOP II.

Evalúa si el proceso es VIABLE para este perfil:
{PERFIL}

Debes analizar dos cosas:
1. Si el objeto es compatible con un abogado persona natural.
2. Si el proceso exige (o es probable que exija) EXPERIENCIA específica,
   certificaciones o RUP. Solo tienes entidad, objeto y presupuesto, no el
   pliego: si el objeto sugiere experiencia (obras, interventoría, consultorías
   especializadas, suministros técnicos) responde "SI"; si es un servicio
   simple que normalmente no la requiere responde "NO"; si no puedes
   determinarlo con la información disponible responde "INCIERTO".

Responde ÚNICAMENTE con un JSON válido, sin texto adicional ni markdown:
{{"compatible": true o false,
"exige_experiencia": "SI" o "NO" o "INCIERTO",
"detalle_experiencia": "1 frase sobre la experiencia/RUP que probablemente se exige",
"motivo": "máximo 2 frases con la razón principal de tu recomendación"}}"""


# ---------------- Utilidades ----------------
def e(x):
    return html.escape(str(x)).replace("\n", " ")


def correo_valido(c):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", (c or "").strip()))


def extraer_url(valor):
    if isinstance(valor, dict):
        return valor.get("url", "")
    return valor or ""


# ---------------- SECOP II ----------------
@st.cache_data(ttl=240, show_spinner=False)
def consultar_secop(regiones, dias, solo_abiertos, limite=500):
    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00")
    filtro_reg = " OR ".join(f"({REGIONES[r]})" for r in regiones)
    where = (
        f"({filtro_reg}) "
        f"AND upper(modalidad_de_contratacion) like '%NIMA CUANT%' "
        f"AND fecha_de_publicacion_del >= '{desde}'"
    )
    if solo_abiertos:
        where += " AND estado_del_procedimiento='Publicado'"
    params = {"$where": where, "$order": "fecha_de_publicacion_del DESC", "$limit": limite}
    r = requests.get(SECOP_URL, params=params, timeout=40)
    r.raise_for_status()
    return pd.DataFrame(r.json())


# ---------------- Claude ----------------
def evaluar_rapido(client, entidad, objeto, presupuesto):
    contenido = (
        f"Entidad: {entidad}\nObjeto del proceso: {objeto}\n"
        f"Presupuesto oficial: ${presupuesto:,.0f} COP"
    )
    try:
        resp = client.messages.create(
            model=MODELO, max_tokens=400, system=SYSTEM_RAPIDO,
            messages=[{"role": "user", "content": contenido}],
        )
        txt = resp.content[0].text.strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        d = json.loads(m.group(0) if m else txt)
        exp = str(d.get("exige_experiencia", "INCIERTO")).upper()
        compatible = bool(d.get("compatible", False))
        if not compatible or exp == "SI":
            estado = "DESCARTAR 🔴"
        elif exp == "NO":
            estado = "VIABLE 🟢"
        else:
            estado = "VERIFICAR 🟡"
        return estado, exp, d.get("detalle_experiencia", ""), d.get("motivo", "")
    except Exception as ex:
        return "ERROR ⚠️", "INCIERTO", "", f"No se pudo evaluar: {ex}"


# ---------------- Correo (Gmail SMTP, gratis) ----------------
def enviar_correo(remitente, clave, destino, asunto, texto, cuerpo_html):
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = destino
    msg.set_content(texto)
    msg.add_alternative(cuerpo_html, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(remitente.strip(), clave.replace(" ", ""))
            s.send_message(msg)
        return True, ""
    except Exception as ex:
        return False, str(ex)


COLORES = {"VIABLE": "#16a34a", "VERIFICAR": "#f59e0b", "DESCARTAR": "#dc2626"}


def color_de(v):
    for k, c in COLORES.items():
        if v.startswith(k):
            return c
    return "#6b7280"


def cargar_enviados():
    if ALERTAS_FILE.exists():
        try:
            return set(json.loads(ALERTAS_FILE.read_text()))
        except Exception:
            pass
    return set()


def procesar_alertas(res):
    permitidos = ("VIABLE", "VERIFICAR") if incluir_verificar else ("VIABLE",)
    candidatos = res[res["Veredicto"].str.startswith(permitidos)]
    enviados = cargar_enviados()
    nuevos = candidatos[~candidatos["ID"].isin(enviados)].head(30)
    if nuevos.empty:
        return

    lineas, tarjetas = [], []
    for _, r in nuevos.iterrows():
        lineas.append(
            f"{r['Veredicto']} {r['Entidad']}\n${r['Presupuesto']:,.0f} COP\n"
            f"{str(r['Objeto'])[:300]}\n{r['Enlace']}\n"
        )
        tarjetas.append(
            f'<div style="border-left:6px solid {color_de(r["Veredicto"])};background:#f9fafb;'
            f'padding:12px 16px;margin:12px 0;border-radius:8px;font-family:Arial,sans-serif">'
            f'<b>{e(r["Entidad"])}</b> — {e(r["Veredicto"])}<br>'
            f'<span style="font-size:18px"><b>${r["Presupuesto"]:,.0f} COP</b></span><br>'
            f'{e(str(r["Objeto"])[:300])}<br><i>{e(r["Motivo"])}</i><br>'
            f'<a href="{e(r["Enlace"])}">Ver pliego en SECOP II</a></div>'
        )

    asunto = f"⚖️ Radar SECOP II: {len(nuevos)} proceso(s) nuevo(s)"
    texto = asunto + "\n\n" + "\n".join(lineas)
    cuerpo = (
        f'<div style="font-family:Arial,sans-serif"><h2>{e(asunto)}</h2>'
        + "".join(tarjetas)
        + "</div>"
    )
    ok, msg = enviar_correo(mail_from, mail_pass, mail_to, asunto, texto, cuerpo)
    if ok:
        enviados.update(nuevos["ID"])
        ALERTAS_FILE.write_text(json.dumps(sorted(enviados)))
        st.toast(f"📧 Correo enviado ({len(nuevos)} procesos)")
    else:
        st.toast(f"No se pudo enviar el correo: {msg}", icon="⚠️")


# ---------------- Búsqueda ----------------
def ejecutar_busqueda():
    if not api_key:
        st.error("Ingresa tu API Key de Anthropic en la barra lateral (🔑).")
        return
    if not regiones:
        st.error("Selecciona al menos una región.")
        return

    with st.spinner("Consultando SECOP II y evaluando con Claude..."):
        try:
            df = consultar_secop(tuple(regiones), dias, solo_abiertos)
        except Exception as ex:
            st.error(f"Error consultando SECOP II: {ex}")
            return

        if df.empty:
            st.session_state["resultados"] = pd.DataFrame()
            return

        df["precio_base"] = pd.to_numeric(df.get("precio_base"), errors="coerce")
        df = df[
            (df["precio_base"] >= pmin * 1_000_000) & (df["precio_base"] <= pmax * 1_000_000)
        ].head(max_eval)

        client = Anthropic(api_key=api_key)
        cache_eval = st.session_state.setdefault("eval_cache", {})
        filas = []
        for _, f in df.iterrows():
            enlace = extraer_url(f.get("urlproceso"))
            pid = str(f.get("id_del_proceso") or enlace)
            objeto = (
                f.get("descripci_n_del_procedimiento")
                or f.get("nombre_del_procedimiento")
                or "Sin descripción"
            )
            entidad = f.get("entidad", "Sin entidad")
            if pid not in cache_eval:
                r = evaluar_rapido(client, entidad, objeto, f["precio_base"])
                if not r[0].startswith("ERROR"):
                    cache_eval[pid] = r
            else:
                r = cache_eval[pid]
            filas.append({
                "ID": pid, "Entidad": entidad, "Objeto": objeto,
                "Presupuesto": f["precio_base"], "Veredicto": r[0],
                "Exige experiencia": r[1], "Detalle experiencia": r[2], "Motivo": r[3],
                "Ciudad": f.get("ciudad_entidad", ""),
                "Departamento": f.get("departamento_entidad", ""),
                "Enlace": enlace,
                "Publicado": str(f.get("fecha_de_publicacion_del", ""))[:10],
            })

    res = pd.DataFrame(filas)
    st.session_state["resultados"] = res
    st.session_state["ultima"] = datetime.now().strftime("%H:%M:%S")
    if alertas_on and not res.empty:
        procesar_alertas(res)


# ---------------- Resultados ----------------
def bienvenida():
    st.markdown(
        """<div class="welcome"><h3>👋 Empieza en 3 pasos</h3>
<p><b>1.</b> Pega tu API Key de Anthropic en la barra lateral (🔑).</p>
<p><b>2.</b> Elige regiones, fechas y presupuesto en el panel de arriba.</p>
<p><b>3.</b> Pulsa <b>Buscar y analizar</b>. Los procesos viables aparecerán en verde 🟢.</p></div>""",
        unsafe_allow_html=True,
    )


def render_resultados():
    res = st.session_state.get("resultados")
    if res is None:
        bienvenida()
        return
    if res.empty:
        st.warning("No hay procesos con esos filtros (región, fechas y presupuesto).")
        return

    n = lambda p: int(res["Veredicto"].str.startswith(p).sum())
    st.markdown(
        f"""<div class="stats">
<div class="stat"><b>{len(res)}</b><span>Analizados</span></div>
<div class="stat"><b>{n('VIABLE')}</b><span>Viables 🟢</span></div>
<div class="stat"><b>{n('VERIFICAR')}</b><span>Verificar 🟡</span></div>
<div class="stat"><b>{n('DESCARTAR')}</b><span>Descartados 🔴</span></div>
</div>""",
        unsafe_allow_html=True,
    )

    k1, k2, k3 = st.columns([2, 1.3, 1.7])
    palabra = k1.text_input("🔍 Buscar palabra clave", placeholder="ej. asesoría jurídica, notificaciones...")
    orden = k2.selectbox(
        "Ordenar por",
        ["Mejor viabilidad", "Más recientes", "Mayor presupuesto", "Menor presupuesto"],
    )
    vista = k3.radio("Vista", ["Tarjetas", "Tabla"], horizontal=True)
    filtro = st.radio(
        "Mostrar", ["Todos", "Solo viables 🟢", "Viables + por verificar 🟢🟡"], horizontal=True
    )

    datos = res.copy()
    if filtro == "Solo viables 🟢":
        datos = datos[datos["Veredicto"].str.startswith("VIABLE")]
    elif filtro.startswith("Viables +"):
        datos = datos[datos["Veredicto"].str.startswith(("VIABLE", "VERIFICAR"))]
    if palabra.strip():
        p = palabra.strip()
        datos = datos[
            datos["Objeto"].str.contains(p, case=False, regex=False)
            | datos["Entidad"].str.contains(p, case=False, regex=False)
        ]
    if orden == "Más recientes":
        datos = datos.sort_values("Publicado", ascending=False)
    elif orden == "Mayor presupuesto":
        datos = datos.sort_values("Presupuesto", ascending=False)
    elif orden == "Menor presupuesto":
        datos = datos.sort_values("Presupuesto", ascending=True)
    else:
        rank = {"VIABLE": 0, "VERIFICAR": 1, "DESCARTAR": 2}
        datos = datos.assign(
            _r=datos["Veredicto"].map(lambda v: next((x for k, x in rank.items() if v.startswith(k)), 3))
        ).sort_values(["_r", "Publicado"], ascending=[True, False]).drop(columns="_r")

    if datos.empty:
        st.info("Ningún proceso coincide con esos filtros.")
    elif vista == "Tabla":
        st.dataframe(
            datos.drop(columns=["ID"]), hide_index=True,
            column_config={
                "Presupuesto": st.column_config.NumberColumn(format="$%d"),
                "Enlace": st.column_config.LinkColumn("Pliego", display_text="Abrir"),
            },
        )
    else:
        tarjetas = []
        for _, r in datos.iterrows():
            boton = (
                f'<a class="btn" href="{e(r["Enlace"])}" target="_blank">📄 Ver pliego en SECOP II</a>'
                if str(r["Enlace"]).startswith("http") else ""
            )
            tarjetas.append(
                f"""<div class="card" style="--c:{color_de(r['Veredicto'])}">
<div class="card-head"><h3>{e(r['Entidad'])}</h3><span class="badge">{e(r['Veredicto'])}</span></div>
<div class="meta">📍 {e(r['Ciudad'])}, {e(r['Departamento'])} · Publicado {e(r['Publicado'])}</div>
<div class="price">&#36;{r['Presupuesto']:,.0f} COP</div>
<p class="obj">{e(r['Objeto'])}</p>
<details><summary>Ver análisis de IA</summary>
<p><b>Objeto completo:</b> {e(r['Objeto'])}</p>
<p><b>¿Exige experiencia?</b> {e(r['Exige experiencia'])} — {e(r['Detalle experiencia'])}</p>
<p><b>Dictamen:</b> {e(r['Motivo'])}</p></details>
{boton}
</div>"""
            )
        st.markdown('<div class="grid">' + "\n".join(tarjetas) + "</div>", unsafe_allow_html=True)

    c1, c2 = st.columns([1, 3])
    c1.download_button(
        "⬇️ Descargar CSV",
        res.drop(columns=["ID"]).to_csv(index=False).encode("utf-8-sig"),
        "resultados_secop.csv", "text/csv",
    )
    if "ultima" in st.session_state:
        c2.caption(f"Última actualización: {st.session_state['ultima']}")


# ---------------- Barra lateral ----------------
with st.sidebar:
    st.header("🔑 Acceso")
    api_key = st.text_input(
        "API Key de Anthropic", type="password", value=os.getenv("ANTHROPIC_API_KEY", "")
    )

    with st.expander("📧 Alertas por correo", expanded=False):
        alertas_on = st.checkbox("Activar notificaciones")
        mail_from = st.text_input("Tu Gmail (remitente)", placeholder="tucorreo@gmail.com")
        mail_pass = st.text_input(
            "Contraseña de aplicación de Google", type="password",
            value=os.getenv("RADAR_MAIL_PASS", ""),
        )
        mail_to = st.text_input("Enviar avisos a", placeholder="(vacío = el mismo Gmail)")
        incluir_verificar = st.checkbox("Avisar también los 🟡 por verificar")
        if st.button("Enviar correo de prueba"):
            destino = (mail_to or mail_from).strip()
            if correo_valido(mail_from) and correo_valido(destino) and mail_pass:
                ok, msg = enviar_correo(
                    mail_from, mail_pass, destino,
                    "✅ Radar SECOP II: prueba de alertas",
                    "Las alertas por correo funcionan.",
                    "<p>✅ Las alertas por correo funcionan.</p>",
                )
                st.success("Enviado, revisa tu bandeja.") if ok else st.error(f"Falló: {msg}")
            else:
                st.error("Revisa el Gmail, la contraseña de aplicación y el destino.")

    with st.expander("🔄 Búsqueda automática"):
        auto = st.checkbox("Buscar cada cierto tiempo")
        intervalo = st.slider("Cada (minutos)", 5, 120, 15)
        st.caption("Funciona mientras esta pestaña esté abierta.")

mail_to = (mail_to or mail_from).strip()
if alertas_on and not (correo_valido(mail_from) and correo_valido(mail_to) and mail_pass):
    alertas_on = False
    st.sidebar.warning("Alertas desactivadas: completa Gmail, contraseña de aplicación y destino.")

# ---------------- Página principal ----------------
st.markdown(
    '<div class="hero"><h1>⚖️ Radar de Licitaciones — SECOP II</h1>'
    "<p>Procesos de Mínima Cuantía evaluados con IA para tu perfil de abogado.</p></div>",
    unsafe_allow_html=True,
)

with st.container(border=True):
    f1, f2 = st.columns([3, 2])
    regiones = f1.multiselect(
        "📍 Regiones", list(REGIONES.keys()), default=["Nariño (todo el departamento)"]
    )
    dias = f2.select_slider(
        "📅 Publicados en los últimos", options=[7, 15, 30, 60, 90, 180], value=30,
        format_func=lambda d: f"{d} días",
    )
    f3, f4 = st.columns([3, 2])
    pmin, pmax = f3.slider("💰 Presupuesto (millones de COP)", 0, 300, (10, 20))
    with f4:
        st.write("")
        buscar = st.button("🚀 Buscar y analizar en SECOP II", type="primary")
    with st.expander("Opciones avanzadas"):
        a1, a2 = st.columns(2)
        solo_abiertos = a1.checkbox("Solo procesos abiertos (Publicado)", value=True)
        max_eval = a2.slider("Máx. procesos a analizar con IA", 1, 50, 15)

if buscar and not auto:
    ejecutar_busqueda()

if auto:
    @st.fragment(run_every=intervalo * 60)
    def panel_auto():
        ejecutar_busqueda()
        render_resultados()

    panel_auto()
else:
    render_resultados()