import os, re, json, uuid, time, io, tempfile, threading
import anthropic, pdfplumber
from flask import Flask, request, jsonify, send_file, render_template_string, abort
from flask_cors import CORS
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from yookassa import Configuration, Payment

app = Flask(__name__)
CORS(app)

# ── Config ──────────────────────────────────────────────
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY","")
YK_SHOP_ID     = os.environ.get("YUKASSA_SHOP_ID","")
YK_SECRET      = os.environ.get("YUKASSA_SECRET_KEY","")
BASE_URL       = os.environ.get("BASE_URL","https://your-app.railway.app")
PRICE_SPEC     = "299.00"
PRICE_NO_SPEC  = "999.00"
PROMO_CODES    = {"AITIMA2024", "LIZA2024"}

Configuration.account_id = YK_SHOP_ID
Configuration.secret_key = YK_SECRET
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

SESSIONS = {}
LOCK = threading.Lock()

def cleanup():
    now = time.time()
    with LOCK:
        old = [k for k,v in SESSIONS.items() if now-v.get("ts",0)>7200]
        for k in old: del SESSIONS[k]

# ── Fonts ───────────────────────────────────────────────
try:
    pdfmetrics.registerFont(TTFont("DV","/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DVB","/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    FONTS_OK = True
except: FONTS_OK = False

# ── PDF Analysis ─────────────────────────────────────────
SPEC_KW = ["спецификац","ведомость","выборка","масса","кол-во","количество",
           "наименование","ед.изм","шт.","кг","м2","м3","бетон","арматур",
           "профил","двутавр","швеллер","уголок","итого","всего","гост"]
NUM_RE = re.compile(r'\b\d{2,}[.,]?\d*\b')
DIM_RE = re.compile(r'\b[0-9]{3,5}\b|[øØ]\d+|t\s*=\s*\d+|\+\d+\.\d+')

def has_spec(text):
    tl=text.lower()
    return sum(1 for k in SPEC_KW if k in tl)>=2 and len(NUM_RE.findall(text))>=3

def has_dims(text):
    return len(DIM_RE.findall(text))>=4

def analyze_pdf(path):
    spec_p, draw_p, total = [], [], 0
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for i,pg in enumerate(pdf.pages):
            txt = pg.extract_text() or ""
            if len(txt.strip())<20: continue
            if has_spec(txt):   spec_p.append({"p":i+1,"t":txt[:3000]})
            elif has_dims(txt): draw_p.append({"p":i+1,"t":txt[:2000]})
    found_spec = len(spec_p)>0
    parts = [f"ПРОЕКТНАЯ ДОКУМЕНТАЦИЯ\nВсего страниц: {total} | Спецификации: {len(spec_p)} | Чертежи: {len(draw_p)}\n{'='*60}\n"]
    if spec_p:
        parts.append("СПЕЦИФИКАЦИИ:\n")
        for p in spec_p: parts.append(f"\n--- Стр.{p['p']} ---\n{p['t']}\n")
    if draw_p:
        parts.append("\nЧЕРТЕЖИ С РАЗМЕРАМИ:\n")
        for p in draw_p[:20]: parts.append(f"\n--- Стр.{p['p']} ---\n{p['t']}\n")
    return found_spec, "\n".join(parts), {"total":total,"spec":len(spec_p),"draw":len(draw_p)}

# ── Claude ───────────────────────────────────────────────
SYS = """Ты — профессиональный сметчик строительных проектов. Создаёшь ВОР из проектной документации.

АЛГОРИТМ:
1. Кратко опиши объект (1-2 предложения)
2. Задай ВСЕ вопросы по двоичности ОДНИМ сообщением:
   - Две единицы измерения у позиции → спроси в чём считать
   - Одна единица → берёшь как есть
3. После ответов → VOR_JSON

ПРАВИЛА: монтаж=те же единицы что материал | антикор=тонны | котлован=усечённая пирамида V=H/6*(AB+ab+(A+a)(B+b)) | профнастил=м²

ФИНАЛЬНЫЙ ВЫВОД строго после ответов:
VOR_JSON:
{"project":"название","code":"шифр","sections":[{"title":"РАЗДЕЛ 1. МАТЕРИАЛЫ","rows":[{"type":"data","no":1,"name":"Двутавр I40Ш1 С245","unit":"кг","qty":"","vol":"8633","note":"Колонны"},{"type":"subtotal","name":"Итого:","unit":"кг","vol":"8633","note":""},{"type":"total","name":"ИТОГО МК:","unit":"т","vol":"99.08","note":""},{"type":"grand","name":"ВСЕГО с коэф.:","unit":"т","vol":"99.08","note":""}]}]}"""

def ai_first(txt):
    r = client.messages.create(model="claude-sonnet-4-6",max_tokens=1000,system=SYS,
        messages=[{"role":"user","content":txt+"\n\nПроанализируй. Опиши объект, задай вопросы по двоичности. Если вопросов нет — сразу VOR_JSON."}])
    return r.content[0].text

def ai_chat(hist):
    r = client.messages.create(model="claude-sonnet-4-6",max_tokens=1000,system=SYS,messages=hist)
    return r.content[0].text

# ── PDF Generation ───────────────────────────────────────
C={"dark":colors.HexColor("#0A2240"),"hdr":colors.HexColor("#1A3A5C"),
   "sub2":colors.HexColor("#D6E4F0"),"yel":colors.HexColor("#FFF3CD"),
   "alt":colors.HexColor("#F5F9FF"),"wht":colors.white,
   "cyan":colors.HexColor("#00E5FF"),"navy":colors.HexColor("#003366"),
   "red":colors.HexColor("#CC0000"),"grey":colors.HexColor("#666666"),
   "info":colors.HexColor("#EBF5FB")}

def mkp(text,fn="DV",sz=8,col=colors.black,align=0):
    fn_r=(fn if FONTS_OK else("Helvetica-Bold" if fn=="DVB" else"Helvetica"))
    return Paragraph(str(text) if text else "",
        ParagraphStyle("_",fontName=fn_r,fontSize=sz,textColor=col,alignment=align,leading=sz+2))

def gen_pdf(data):
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=landscape(A4),
        leftMargin=10*mm,rightMargin=10*mm,topMargin=10*mm,bottomMargin=14*mm)
    CW=[9*mm,112*mm,17*mm,20*mm,20*mm,69*mm]
    rows,stys=[],[]
    def add(r,s): rows.append(r);stys.append(s)
    add([mkp("ВЕДОМОСТЬ ОБЪЁМОВ РАБОТ И МАТЕРИАЛОВ","DVB",13,C["wht"],1),"","","","",""],"TTL")
    add([mkp(f"Объект: {data.get('project','—')}   |   Шифр: {data.get('code','—')}",sz=8,col=colors.HexColor("#444444")),"","","","",""],"INF")
    add([mkp("№","DVB",8,C["wht"],1),mkp("Наименование работ и материалов","DVB",8,C["wht"]),
         mkp("Ед.изм.","DVB",8,C["wht"],1),mkp("Кол-во","DVB",8,C["wht"],1),
         mkp("Масса / объём","DVB",8,C["wht"],1),mkp("Примечание","DVB",8,C["wht"])],"CHD")
    num=[0];alt=[False]
    for sec in data.get("sections",[]):
        add([mkp(sec.get("title",""),"DVB",9,C["cyan"]),"","","","",""],"SEC");alt[0]=False
        for row in sec.get("rows",[]):
            t=row.get("type","data")
            bg=C["alt"] if alt[0] else C["wht"]
            if t=="subtotal": bg=C["sub2"]
            elif t=="total":  bg=C["yel"]
            elif t=="grand":  bg=C["dark"]
            nc=C["cyan"] if t=="grand" else C["navy"] if t in("subtotal","total") else colors.black
            vc=C["cyan"] if t=="grand" else C["red"] if t=="total" else C["navy"]
            if t=="data": num[0]+=1;no=mkp(str(num[0]),sz=7,col=C["grey"],align=1)
            else: no=mkp("")
            fn="DVB" if t!="data" else "DV"
            add([no,mkp(row.get("name",""),fn,8 if t!="data" else 7.5,nc),
                 mkp(row.get("unit",""),sz=7.5,align=1),
                 mkp(str(row.get("qty","")) if row.get("qty") else "",sz=7.5,align=2),
                 mkp(str(row.get("vol","")) if row.get("vol") else "","DVB",7.5,vc,2),
                 mkp(str(row.get("note","")) if row.get("note") else "",sz=7,col=C["grey"])],t)
            if t=="data": alt[0]=not alt[0]
    cmds=[("FONTNAME",(0,0),(-1,-1),"DV" if FONTS_OK else "Helvetica"),
          ("FONTSIZE",(0,0),(-1,-1),7.5),("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#CCCCCC")),
          ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),3),
          ("RIGHTPADDING",(0,0),(-1,-1),3),("TOPPADDING",(0,0),(-1,-1),2),("BOTTOMPADDING",(0,0),(-1,-1),2)]
    for i,st in enumerate(stys):
        if st=="TTL":   cmds+=[("BACKGROUND",(0,i),(5,i),C["hdr"]),("SPAN",(0,i),(5,i)),("TOPPADDING",(0,i),(5,i),8),("BOTTOMPADDING",(0,i),(5,i),8)]
        elif st=="INF": cmds+=[("BACKGROUND",(0,i),(5,i),C["info"]),("SPAN",(0,i),(5,i))]
        elif st=="CHD": cmds+=[("BACKGROUND",(0,i),(5,i),C["hdr"]),("ALIGN",(0,i),(5,i),"CENTER"),("TOPPADDING",(0,i),(5,i),4),("BOTTOMPADDING",(0,i),(5,i),4)]
        elif st=="SEC": cmds+=[("BACKGROUND",(0,i),(5,i),C["dark"]),("SPAN",(0,i),(5,i)),("TOPPADDING",(0,i),(5,i),4),("BOTTOMPADDING",(0,i),(5,i),4)]
        elif st=="subtotal": cmds+=[("BACKGROUND",(0,i),(5,i),C["sub2"]),("SPAN",(0,i),(1,i))]
        elif st=="total":    cmds+=[("BACKGROUND",(0,i),(5,i),C["yel"]),("SPAN",(0,i),(1,i)),("TOPPADDING",(0,i),(5,i),3),("BOTTOMPADDING",(0,i),(5,i),3)]
        elif st=="grand":    cmds+=[("BACKGROUND",(0,i),(5,i),C["dark"]),("SPAN",(0,i),(1,i)),("TOPPADDING",(0,i),(5,i),4),("BOTTOMPADDING",(0,i),(5,i),4)]
        elif st=="DA":       cmds+=[("BACKGROUND",(0,i),(5,i),C["alt"])]
    def footer(cv,dc):
        cv.saveState();cv.setFont("DV" if FONTS_OK else "Helvetica",7)
        cv.setFillColor(colors.HexColor("#888888"))
        cv.drawString(10*mm,7*mm,"АйТима — Генератор ВОР")
        cv.drawRightString(287*mm,7*mm,f"Стр. {dc.page}");cv.restoreState()
    t=Table(rows,colWidths=CW,repeatRows=3);t.setStyle(TableStyle(cmds))
    doc.build([t],onFirstPage=footer,onLaterPages=footer)
    return buf.getvalue()

def parse_vor(text):
    idx=text.find("VOR_JSON:")+9
    raw=text[idx:].strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

# ══════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════

@app.route("/")
def index():
    return open(os.path.join(os.path.dirname(__file__),"templates","index.html"),encoding="utf-8").read()

@app.route("/health")
def health():
    return jsonify({"ok":True,"fonts":FONTS_OK})

@app.route("/upload",methods=["POST"])
def upload():
    cleanup()
    if "file" not in request.files: return jsonify({"error":"Файл не загружен"}),400
    f=request.files["file"]
    if not f.filename.lower().endswith(".pdf"): return jsonify({"error":"Только PDF"}),400
    with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as tmp:
        f.save(tmp.name);path=tmp.name
    try:
        found_spec,content,stats=analyze_pdf(path)
        os.unlink(path)
    except Exception as e:
        try: os.unlink(path)
        except: pass
        return jsonify({"error":str(e)}),500
    try: reply=ai_first(content)
    except Exception as e: return jsonify({"error":"Claude: "+str(e)}),500
    has_vor="VOR_JSON:" in reply
    sid=str(uuid.uuid4())
    hist=[
        {"role":"user","content":content+"\n\nПроанализируй. Опиши объект, задай вопросы по двоичности. Если нет — сразу VOR_JSON."},
        {"role":"assistant","content":reply}
    ]
    with LOCK:
        SESSIONS[sid]={"status":"chat" if not has_vor else "ready","doc_type":"spec" if found_spec else "drawing",
                       "price":PRICE_SPEC if found_spec else PRICE_NO_SPEC,"history":hist,
                       "vor_text":reply if has_vor else None,"pdf_bytes":None,"ts":time.time()}
    return jsonify({"session_id":sid,"reply":reply,"has_vor":has_vor,
                    "doc_type":"spec" if found_spec else "drawing",
                    "price":PRICE_SPEC if found_spec else PRICE_NO_SPEC,"stats":stats})

@app.route("/chat",methods=["POST"])
def chat():
    d=request.json;sid=d.get("session_id","");msg=d.get("message","").strip()
    with LOCK: sess=SESSIONS.get(sid)
    if not sess: return jsonify({"error":"Сессия не найдена"}),404
    if not msg:  return jsonify({"error":"Пустое сообщение"}),400
    sess["history"].append({"role":"user","content":msg})
    try: reply=ai_chat(sess["history"])
    except Exception as e: return jsonify({"error":str(e)}),500
    sess["history"].append({"role":"assistant","content":reply})
    has_vor="VOR_JSON:" in reply
    if has_vor: sess["vor_text"]=reply;sess["status"]="ready"
    return jsonify({"reply":reply,"has_vor":has_vor})

@app.route("/create-payment",methods=["POST"])
def create_payment():
    d=request.json;sid=d.get("session_id","");promo=d.get("promo","").strip().upper()
    with LOCK: sess=SESSIONS.get(sid)
    if not sess: return jsonify({"error":"Сессия не найдена"}),404
    if not sess.get("vor_text"): return jsonify({"error":"ВОР ещё не готова"}),400

    # Promo code — skip payment, generate PDF directly
    if promo in PROMO_CODES:
        try:
            pdf=gen_pdf(parse_vor(sess["vor_text"]))
            sess["pdf_bytes"]=pdf;sess["status"]="paid";sess["promo"]=promo
            return jsonify({"promo_ok":True,"session_id":sid})
        except Exception as e: return jsonify({"error":"Ошибка генерации: "+str(e)}),500

    desc=("Генерация ВОР со спецификацией" if sess["doc_type"]=="spec" else "Генерация ВОР по чертежам")
    try:
        pay=Payment.create({"amount":{"value":sess["price"],"currency":"RUB"},
            "confirmation":{"type":"redirect","return_url":f"{BASE_URL}/payment-result?session_id={sid}"},
            "capture":True,"description":desc,"metadata":{"session_id":sid}},str(uuid.uuid4()))
        sess["payment_id"]=pay.id;sess["status"]="pending"
        return jsonify({"payment_url":pay.confirmation.confirmation_url,"payment_id":pay.id})
    except Exception as e: return jsonify({"error":"Ошибка оплаты: "+str(e)}),500

@app.route("/webhook/yukassa",methods=["POST"])
def webhook():
    try:
        body=request.json
        if body.get("type")!="notification": return "ok",200
        obj=body.get("object",{});status=obj.get("status");sid=obj.get("metadata",{}).get("session_id","")
        if status=="succeeded" and sid:
            with LOCK: sess=SESSIONS.get(sid)
            if sess:
                try:
                    pdf=gen_pdf(parse_vor(sess["vor_text"]))
                    sess["pdf_bytes"]=pdf;sess["status"]="paid"
                except Exception as e:
                    sess["status"]="error";sess["error"]=str(e)
    except: pass
    return "ok",200

@app.route("/payment-result")
def payment_result():
    sid=request.args.get("session_id","")
    html=open(os.path.join(os.path.dirname(__file__),"templates","result.html"),encoding="utf-8").read()
    return html.replace("__SID__",sid)

@app.route("/status/<sid>")
def status(sid):
    with LOCK: sess=SESSIONS.get(sid)
    if not sess: return jsonify({"status":"not_found"}),404
    return jsonify({"status":sess["status"],"error":sess.get("error","")})

@app.route("/download/<sid>")
def download(sid):
    with LOCK: sess=SESSIONS.get(sid)
    if not sess: abort(404)
    if sess["status"]!="paid" or not sess.get("pdf_bytes"): abort(403)
    return send_file(io.BytesIO(sess["pdf_bytes"]),mimetype="application/pdf",
                     as_attachment=True,download_name=f"VOR_{sid[:8]}.pdf")

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
