#!/usr/bin/env python3
"""
Metsäkylän päivä – rakentaa päivittäisen HTML-sivun:
  1. Ilmatieteen laitoksen ennuste (Metsäkylä, Ylöjärvi)
  2. Pörssisähkön tuntihinnat (porssisahko.net, sis. alv)
  3. Edellisillan NHL-ottelut, joissa pelasi suomalaisia, ja heidän tehopisteensä

Vaatii Python 3.9+ (vain vakiokirjasto). Ei API-avaimia.

Käyttö:
    python3 paivan_sivu.py /var/www/html/index.html

Esimerkki cron-rivistä (joka aamu klo 7.15 ja uudelleen klo 14.30, kun
seuraavan päivän sähkönhinnat ja päivitetty ennuste ovat tulleet):
    CRON_TZ=Europe/Helsinki
    15 7 * * *  /usr/bin/python3 /opt/paivan_sivu/paivan_sivu.py /var/www/html/index.html
    30 14 * * * /usr/bin/python3 /opt/paivan_sivu/paivan_sivu.py /var/www/html/index.html
"""

import html
import json
import math
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- asetukset
PAIKKA = "Metsäkylä,Ylöjärvi"      # FMI:n paikannimi
LATLON = None                       # esim. "61.55,23.57" – käytetään paikannimen sijaan jos asetettu
SIVUN_OTSIKKO = "Metsäkylän päivä"
HKI = ZoneInfo("Europe/Helsinki")
UA = {"User-Agent": "metsakyla-paivasivu/1.0"}
TIMEOUT = 25
CACHE_NIMI = "nhl_kansallisuudet.json"   # tallentuu skriptin viereen

FIN_VIIKONPAIVAT = ["Maanantai", "Tiistai", "Keskiviikko", "Torstai", "Perjantai", "Lauantai", "Sunnuntai"]

SYMBOLIT = {
    1: "selkeää", 2: "puolipilvistä", 3: "pilvistä",
    21: "heikkoja sadekuuroja", 22: "sadekuuroja", 23: "voimakkaita sadekuuroja",
    31: "heikkoa vesisadetta", 32: "vesisadetta", 33: "voimakasta vesisadetta",
    41: "heikkoja lumikuuroja", 42: "lumikuuroja", 43: "voimakkaita lumikuuroja",
    51: "heikkoa lumisadetta", 52: "lumisadetta", 53: "voimakasta lumisadetta",
    61: "ukkoskuuroja", 62: "voimakkaita ukkoskuuroja", 63: "ukkosta", 64: "voimakasta ukkosta",
    71: "heikkoja räntäkuuroja", 72: "räntäkuuroja", 73: "voimakkaita räntäkuuroja",
    81: "heikkoa räntäsadetta", 82: "räntäsadetta", 83: "voimakasta räntäsadetta",
    91: "utua", 92: "sumua",
}
SUUNNAT = ["pohjoinen", "koillinen", "itä", "kaakko", "etelä", "lounas", "länsi", "luode"]
NUOLET = ["↓", "↙", "←", "↖", "↑", "↗", "→", "↘"]   # nuoli osoittaa tuulen menosuuntaan


def hae(url, json_=True):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()
    return json.loads(data) if json_ else data


def luku(x, des=0):
    """Suomalainen numeromuoto: pilkku, oikea miinusmerkki."""
    s = f"{x:.{des}f}".replace(".", ",")
    return s.replace("-", "−")


# ---------------------------------------------------------------- 1. sää
def hae_saa(nyt):
    alku = nyt.replace(minute=0, second=0, microsecond=0)
    loppu = nyt.replace(hour=23, minute=0, second=0, microsecond=0)
    if loppu <= alku:
        loppu = alku + timedelta(hours=6)
    paikka = {"latlon": LATLON} if LATLON else {"place": PAIKKA}
    perus = {
        "service": "WFS", "version": "2.0.0", "request": "getFeature",
        "storedquery_id": "fmi::forecast::edited::weather::scandinavia::point::simple",
        "starttime": alku.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endtime": loppu.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestep": "60", **paikka,
    }
    # Yritetään ensin laajalla parametrilistalla, sitten varmalla perusjoukolla.
    listat = [
        "Temperature,WindSpeedMS,WindDirection,HourlyMaximumGust,Precipitation1h,PoP,WeatherSymbol3",
        "Temperature,WindSpeedMS,WindDirection,Precipitation1h,WeatherSymbol3",
        "Temperature,WindSpeedMS,WindDirection,Precipitation1h",
    ]
    viimeisin = None
    for p in listat:
        try:
            url = "https://opendata.fmi.fi/wfs?" + urllib.parse.urlencode({**perus, "parameters": p})
            raaka = hae(url, json_=False)
            break
        except Exception as e:  # noqa
            viimeisin = e
    else:
        raise RuntimeError(f"FMI-haku epäonnistui: {viimeisin}")

    ns = {"BsWfs": "http://xml.fmi.fi/schema/wfs/2.0"}
    rivit = {}
    for el in ET.fromstring(raaka).iter("{http://xml.fmi.fi/schema/wfs/2.0}BsWfsElement"):
        t = el.find("BsWfs:Time", ns).text
        nimi = el.find("BsWfs:ParameterName", ns).text
        arvo = el.find("BsWfs:ParameterValue", ns).text
        try:
            v = float(arvo)
            if math.isnan(v):
                v = None
        except (TypeError, ValueError):
            v = None
        aika = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(HKI)
        rivit.setdefault(aika, {})[nimi] = v

    tunnit = []
    for aika in sorted(rivit):
        r = rivit[aika]
        if r.get("Temperature") is None:
            continue
        t, w = r["Temperature"], r.get("WindSpeedMS") or 0
        tunnit.append({
            "h": aika.hour, "t": t, "tk": tuntuu_kuin(t, w), "w": w,
            "d": r.get("WindDirection"), "g": r.get("HourlyMaximumGust"),
            "sade": r.get("Precipitation1h"), "pop": r.get("PoP"),
            "sym": r.get("WeatherSymbol3"),
        })
    if not tunnit:
        raise RuntimeError("FMI palautti tyhjän ennusteen")
    return tunnit


def tuntuu_kuin(t, w):
    """Laskennallinen pakkasen purevuus (JAG/TI-kaava); lämpimällä sama kuin lämpötila."""
    if t > 10 or w < 1.4:
        return t
    v = (w * 3.6) ** 0.16
    return 13.12 + 0.6215 * t - 11.37 * v + 0.3965 * t * v


def saa_html(tunnit):
    lt = [x["t"] for x in tunnit]
    tuulet = [x["w"] for x in tunnit]
    sade_yht = sum(x["sade"] or 0 for x in tunnit)
    puuskat = [x["g"] for x in tunnit if x["g"] is not None]
    pops = [x["pop"] for x in tunnit if x["pop"] is not None]
    ylin = max(tunnit, key=lambda x: x["t"])
    viim = tunnit[-1]

    symbolit = [SYMBOLIT.get(int(x["sym"])) for x in tunnit if x["sym"]]
    yleisin = max(set(symbolit), key=symbolit.count) if symbolit else None
    suunnat = [x["d"] for x in tunnit if x["d"] is not None]
    pääsuunta = SUUNNAT[round(sum(suunnat) / len(suunnat) / 45) % 8] if suunnat else None

    lause = []
    if yleisin:
        lause.append(f"Loppupäivä on enimmäkseen {yleisin}.")
    if lt[0] - lt[-1] >= 2:
        lause.append(f"Lämpötila laskee {luku(lt[0])} asteesta {luku(lt[-1])} asteeseen klo {viim['h']} mennessä.")
    elif lt[-1] - lt[0] >= 2:
        lause.append(f"Lämpötila nousee {luku(lt[0])} asteesta {luku(lt[-1])} asteeseen.")
    else:
        lause.append(f"Lämpötila pysyy {luku(min(lt))}–{luku(max(lt))} asteessa.")
    lause.append(("Sadetta ei ole odotettavissa." if sade_yht < 0.1 else f"Sadetta kertyy noin {luku(sade_yht, 1)} mm."))
    if pääsuunta:
        lause.append(f"Tuuli on enimmäkseen {pääsuunta}tuulta, {luku(min(tuulet))}–{luku(max(tuulet))} m/s.")

    def suunta(d):
        if d is None:
            return ""
        i = round(d / 45) % 8
        return f'<span class="wind-arrow" aria-hidden="true">{NUOLET[i]}</span> {SUUNNAT[i]} '

    rivit = []
    for x in tunnit:
        g = f" ({luku(x['g'])})" if x["g"] is not None else ""
        pop = f"{luku(x['pop'])} % · " if x["pop"] is not None else ""
        sym = SYMBOLIT.get(int(x["sym"]), "") if x["sym"] else ""
        rivit.append(
            f'<tr><td class="num">{x["h"]:02d}.00</td><td>{html.escape(sym)}</td>'
            f'<td class="r num">{luku(x["t"])}°</td><td class="r num">{luku(x["tk"])}°</td>'
            f'<td>{suunta(x["d"])}<span class="num">{luku(x["w"])}{g} m/s</span></td>'
            f'<td class="r num">{pop}{luku(x["sade"] or 0, 1)}</td></tr>')

    puuska_s = f"puuskat max {luku(max(puuskat))} m/s" if puuskat else "&nbsp;"
    pop_s = f"todennäköisyys max {luku(max(pops))} %" if pops else "kertymä"
    return f"""
    <p class="lead">{' '.join(lause)}</p>
    <div class="stats">
      <div class="stat"><span class="k">Ylin</span><span class="v">{luku(ylin['t'])}°</span><span class="s">klo {ylin['h']}</span></div>
      <div class="stat"><span class="k">Klo {viim['h']}</span><span class="v">{luku(viim['t'])}°</span><span class="s">tuntuu kuin {luku(viim['tk'])}°</span></div>
      <div class="stat"><span class="k">Tuuli</span><span class="v">{luku(min(tuulet))}–{luku(max(tuulet))} m/s</span><span class="s">{puuska_s}</span></div>
      <div class="stat"><span class="k">Sade</span><span class="v">{luku(sade_yht, 1)} mm</span><span class="s">{pop_s}</span></div>
    </div>
    <div class="scroll"><table>
      <thead><tr><th>Klo</th><th>Sää</th><th class="r">Lämpötila</th><th class="r">Tuntuu kuin</th><th>Tuuli (puuskat)</th><th class="r">Sade mm</th></tr></thead>
      <tbody>{''.join(rivit)}</tbody>
    </table></div>"""


# ---------------------------------------------------------------- 2. sähkö
def hae_sahko(nyt):
    data = hae("https://api.porssisahko.net/v2/latest-prices.json")
    paiva = nyt.date()
    tunnit = {}
    for p in data.get("prices", []):
        alku = datetime.fromisoformat(p["startDate"].replace("Z", "+00:00")).astimezone(HKI)
        if alku.date() == paiva:
            tunnit.setdefault(alku.hour, []).append(float(p["price"]))
    if len(tunnit) < 20:
        raise RuntimeError("Tämän päivän sähkönhintoja ei löytynyt")
    # Kesäaikasiirtymän päivä voi olla 23 tai 25 tuntia; näytetään olemassa olevat tunnit.
    return [(h, sum(v) / len(v)) for h, v in sorted(tunnit.items())]


def sahko_html(hinnat):
    arvot = [v for _, v in hinnat]
    ka = sum(arvot) / len(arvot)
    hmin = min(hinnat, key=lambda x: x[1])
    hmax = max(hinnat, key=lambda x: x[1])
    kalliit = [h for h, v in hinnat if v >= 15]
    halvat_paiva = [h for h, v in hinnat if 7 <= h <= 21 and v < 5]

    lause = [f"Päivän keskihinta on {luku(ka, 2)} c/kWh."]
    if kalliit:
        lause.append(f"Kallista (yli 15 c) on klo {jaksot(kalliit)}.")
    else:
        lause.append("Yksikään tunti ei nouse yli 15 sentin.")
    if halvat_paiva:
        lause.append(f"Päiväsaikaan halvinta (alle 5 c) on klo {jaksot(halvat_paiva)}.")

    data_js = json.dumps([[h, round(v, 2)] for h, v in hinnat])
    return f"""
    <p class="lead">{' '.join(lause)}</p>
    <div class="stats">
      <div class="stat"><span class="k">Keskihinta</span><span class="v">{luku(ka, 2)}</span><span class="s">c/kWh</span></div>
      <div class="stat"><span class="k">Halvin</span><span class="v" style="color:{vari(hmin[1])}">{luku(hmin[1], 2)}</span><span class="s">klo {hmin[0]}–{hmin[0] + 1}</span></div>
      <div class="stat"><span class="k">Kallein</span><span class="v" style="color:{vari(hmax[1])}">{luku(hmax[1], 2)}</span><span class="s">klo {hmax[0]}–{hmax[0] + 1}</span></div>
      <div class="stat"><span class="k">Nyt</span><span class="v" id="nowV">–</span><span class="s" id="nowS">&nbsp;</span></div>
    </div>
    <div class="chart-wrap"><svg id="chart" viewBox="0 0 720 240" width="100%" role="img" aria-label="Pörssisähkön tuntihinnat"></svg></div>
    <div class="legend">
      <span><i style="background:var(--cheap)"></i>alle 5 c</span>
      <span><i style="background:var(--mid)"></i>5–15 c</span>
      <span><i style="background:var(--high)"></i>yli 15 c</span>
    </div>
    <script>window.SAHKO={data_js};</script>"""


def vari(v):
    return "var(--cheap)" if v < 5 else "var(--mid)" if v < 15 else "var(--high)"


def jaksot(tunnit):
    """[17,18,19,22] -> '17–20 ja 22–23'"""
    osat, alku, ed = [], tunnit[0], tunnit[0]
    for h in tunnit[1:] + [None]:
        if h is not None and h == ed + 1:
            ed = h
            continue
        osat.append(f"{alku}–{ed + 1}")
        if h is not None:
            alku = ed = h
    return ", ".join(osat[:-1]) + (" ja " if len(osat) > 1 else "") + osat[-1]


# ---------------------------------------------------------------- 3. NHL
def lataa_cache(polku):
    try:
        with open(polku, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def kansallisuus(pid, cache):
    k = str(pid)
    if k not in cache:
        try:
            d = hae(f"https://api-web.nhle.com/v1/player/{pid}/landing")
            cache[k] = d.get("birthCountry") or ""
        except Exception:
            return ""   # ei tallenneta virhettä, yritetään uudelleen ensi kerralla
    return cache[k]


def nimi(n):
    return n.get("default", "") if isinstance(n, dict) else (n or "")


def hae_nhl(nyt, cache):
    pvm = (nyt.date() - timedelta(days=1)).isoformat()   # edellinen ilta Pohjois-Amerikassa
    data = hae(f"https://api-web.nhle.com/v1/score/{pvm}")
    ottelut = []
    for g in data.get("games", []):
        if g.get("gameState") not in ("OFF", "FINAL"):
            continue
        box = hae(f"https://api-web.nhle.com/v1/gamecenter/{g['id']}/boxscore")
        pbs = box.get("playerByGameStats", {})
        suomalaiset = []
        for puoli in ("awayTeam", "homeTeam"):
            abbr = g[puoli]["abbrev"]
            for ryhma in ("forwards", "defense", "goalies"):
                for p in pbs.get(puoli, {}).get(ryhma, []):
                    if kansallisuus(p["playerId"], cache) != "FIN":
                        continue
                    suomalaiset.append({
                        "nimi": nimi(p.get("name")), "joukkue": abbr, "mv": ryhma == "goalies",
                        "m": p.get("goals", 0), "s": p.get("assists", 0),
                        "pm": p.get("plusMinus", 0), "toi": (p.get("toi") or "").replace(":", "."),
                        "torjunnat": p.get("saveShotsAgainst"),
                    })
        if not suomalaiset:
            continue
        tyyppi = (g.get("gameOutcome") or {}).get("lastPeriodType") or (g.get("periodDescriptor") or {}).get("periodType")
        ottelut.append({
            "koti": nimi(g["homeTeam"].get("name")) or g["homeTeam"]["abbrev"],
            "vieras": nimi(g["awayTeam"].get("name")) or g["awayTeam"]["abbrev"],
            "km": g["homeTeam"].get("score", 0), "vm": g["awayTeam"].get("score", 0),
            "lisa": {"OT": " ja.", "SO": " vl."}.get(tyyppi, ""),
            "pelaajat": sorted(suomalaiset, key=lambda x: (-(x["m"] + x["s"]), -x["m"])),
        })
    kaikki = len([g for g in data.get("games", []) if g.get("gameState") in ("OFF", "FINAL")])
    return pvm, kaikki, ottelut


def nhl_html(pvm, kaikki, ottelut):
    d = datetime.fromisoformat(pvm)
    otsikko_pvm = f"{d.day}.{d.month}."
    if kaikki == 0:
        return f'<p class="lead">NHL:ssä ei pelattu otteluita {otsikko_pvm} illalla.</p>', otsikko_pvm
    if not ottelut:
        return f'<p class="lead">Illan {kaikki} ottelussa ei pelannut suomalaisia.</p>', otsikko_pvm

    pisteilla = [(p["nimi"], p["m"], p["s"]) for o in ottelut for p in o["pelaajat"] if p["m"] + p["s"] > 0]
    if pisteilla:
        paras = max(pisteilla, key=lambda x: (x[1] + x[2], x[1]))
        lead = (f"Illan {kaikki} ottelusta suomalaisia pelasi {len(ottelut)}:ssä, ja pisteille ylsi "
                f"{len(pisteilla)} suomalaista. Tehokkain oli {html.escape(paras[0])} ({paras[1]}+{paras[2]}).")
    else:
        lead = f"Illan {kaikki} ottelusta suomalaisia pelasi {len(ottelut)}:ssä. Yksikään suomalainen ei tehnyt pisteitä."

    kortit = []
    for o in ottelut:
        voittaja_koti = o["km"] > o["vm"]
        koti = f"<b>{html.escape(o['koti'])}</b>" if voittaja_koti else html.escape(o["koti"])
        vieras = html.escape(o["vieras"]) if voittaja_koti else f"<b>{html.escape(o['vieras'])}</b>"
        rivit = []
        for p in o["pelaajat"]:
            pts = p["m"] + p["s"]
            if p["mv"]:
                tilasto = f'<td class="r num" colspan="3">torjunnat {html.escape(str(p["torjunnat"] or "–"))}</td>'
            else:
                pm = p["pm"]
                pmc = "pm-pos" if pm > 0 else "pm-neg" if pm < 0 else ""
                pms = f"+{pm}" if pm > 0 else ("±0" if pm == 0 else f"−{abs(pm)}")
                tilasto = (f'<td class="r num">{p["m"]}+{p["s"]}</td>'
                           f'<td class="r"><span class="pts{" has" if pts else ""}">{pts}</span></td>'
                           f'<td class="r num {pmc}">{pms}</td>')
            rivit.append(f'<tr><td>{html.escape(p["nimi"])}{" (MV)" if p["mv"] else ""}</td>'
                         f'<td>{html.escape(p["joukkue"])}</td>{tilasto}<td class="r num">{html.escape(p["toi"])}</td></tr>')
        kortit.append(f"""
      <article class="game">
        <div class="score"><span class="teams">{koti} – {vieras}</span><span class="result num">{o['km']}–{o['vm']}{o['lisa']}</span></div>
        <div class="scroll"><table>
          <thead><tr><th>Pelaaja</th><th>Joukkue</th><th class="r">M+S</th><th class="r">Pisteet</th><th class="r">+/−</th><th class="r">Peliaika</th></tr></thead>
          <tbody>{''.join(rivit)}</tbody>
        </table></div>
      </article>""")
    return f'<p class="lead">{lead}</p><div class="games">{"".join(kortit)}</div>', otsikko_pvm


# ---------------------------------------------------------------- sivu
def virhe_html(teksti, e):
    return f'<p class="lead error">{teksti} Yritetään uudelleen seuraavalla ajokerralla. <span class="src">({html.escape(str(e)[:160])})</span></p>'


def rakenna(nyt, saa, sahko, nhl, nhl_pvm):
    paivays = f"{FIN_VIIKONPAIVAT[nyt.weekday()]} {nyt.day}.{nyt.month}.{nyt.year}"
    paivitetty = f"{nyt.day}.{nyt.month}. klo {nyt.hour}.{nyt.minute:02d}"
    return SIVU.format(
        otsikko=SIVUN_OTSIKKO, paivays=paivays, iso=nyt.date().isoformat(),
        paikka=html.escape(PAIKKA.replace(",", ", ")), saa=saa, sahko=sahko, nhl=nhl,
        nhl_pvm=nhl_pvm, paivitetty=paivitetty,
    )


def main():
    if len(sys.argv) < 2:
        print("Käyttö: python3 paivan_sivu.py <tulostiedosto.html>")
        sys.exit(1)
    ulos = sys.argv[1]
    nyt = datetime.now(HKI)
    cache_polku = os.path.join(os.path.dirname(os.path.abspath(__file__)), CACHE_NIMI)
    cache = lataa_cache(cache_polku)

    try:
        saa = saa_html(hae_saa(nyt))
    except Exception as e:
        saa = virhe_html("Sääennustetta ei saatu haettua.", e)
    try:
        sahko = sahko_html(hae_sahko(nyt))
    except Exception as e:
        sahko = virhe_html("Sähkön hintoja ei saatu haettua.", e)
    try:
        pvm, kaikki, ottelut = hae_nhl(nyt, cache)
        nhl, nhl_pvm = nhl_html(pvm, kaikki, ottelut)
    except Exception as e:
        nhl, nhl_pvm = virhe_html("NHL-tuloksia ei saatu haettua.", e), "edellisillan"

    try:
        with open(cache_polku, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass

    tmp = ulos + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(rakenna(nyt, saa, sahko, nhl, nhl_pvm))
    os.replace(tmp, ulos)   # vaihto kerralla, ettei lukija näe puolikasta sivua
    print(f"Kirjoitettu {ulos} ({nyt:%d.%m.%Y %H.%M})")


SIVU = """<!doctype html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{otsikko}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=Source+Sans+3:wght@400;600;700&family=JetBrains+Mono:wght@500;700&display=swap">
<style>
:root{{
  --bg:#eef2f4; --surface:#ffffff; --fg:#17232b; --muted:#5b6b76; --line:#d5dee3;
  --accent:#1f6f8b; --accent-soft:#dcecf2;
  --cheap:#2f8a57; --mid:#c08a12; --high:#c2413a;
  --display:"Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --body:"Source Sans 3", "Segoe UI", system-ui, sans-serif;
  --mono:"JetBrains Mono", ui-monospace, Menlo, monospace;
  color-scheme:light;
}}
@media (prefers-color-scheme: dark){{:root{{
  --bg:#10181d; --surface:#18232a; --fg:#e4ecf0; --muted:#94a6b1; --line:#2a3a44;
  --accent:#6cc1dd; --accent-soft:#1d3742;
  --cheap:#5cc48a; --mid:#e2b04a; --high:#f07a72; color-scheme:dark;}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 var(--body);
  padding-left:max(16px, env(safe-area-inset-left, 0px));padding-right:max(16px, env(safe-area-inset-right, 0px));padding-block:28px 48px}}
main{{max-width:860px;margin:0 auto;display:grid;gap:28px}}
header{{display:grid;gap:4px}}
.date{{font:600 .8rem var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}}
h1{{font:800 clamp(1.8rem,5vw,2.6rem)/1.1 var(--display);margin:0;text-wrap:balance}}
h2{{font:600 1.35rem/1.2 var(--display);margin:0;text-wrap:balance}}
section{{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:20px;display:grid;gap:16px;min-width:0}}
.head{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:6px 16px}}
.src{{font-size:.82rem;color:var(--muted)}}
a{{color:var(--accent)}}
.lead{{margin:0;font-size:1.05rem;max-width:65ch}}
.error{{color:var(--high)}}
.num{{font-family:var(--mono);font-variant-numeric:tabular-nums}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.stat{{border:1px solid var(--line);border-radius:8px;padding:10px 12px;display:grid;gap:2px}}
.stat .k{{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}}
.stat .v{{font:700 1.3rem var(--mono);font-variant-numeric:tabular-nums}}
.stat .s{{font-size:.82rem;color:var(--muted)}}
.scroll,.chart-wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:.92rem}}
th,td{{padding:7px 8px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}}
td.r,th.r{{text-align:right}}
.wind-arrow{{display:inline-block;width:1em;text-align:center;color:var(--accent)}}
svg text{{fill:var(--muted);font:11px var(--mono)}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;font-size:.82rem;color:var(--muted)}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}}
.games{{display:grid;gap:14px}}
.game{{border:1px solid var(--line);border-radius:8px;overflow:hidden}}
.score{{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding:12px 14px;background:var(--accent-soft)}}
.teams{{font:600 1.08rem var(--display)}}
.teams b{{font-weight:800}}
.result{{font:700 1.3rem var(--mono)}}
.pts{{display:inline-block;min-width:3.4em;text-align:center;font:700 .95rem var(--mono);padding:2px 6px;border-radius:5px;background:var(--bg);border:1px solid var(--line)}}
.pts.has{{background:var(--accent);color:var(--surface);border-color:var(--accent)}}
.pm-pos{{color:var(--cheap)}} .pm-neg{{color:var(--high)}}
.foot{{font-size:.82rem;color:var(--muted);margin:0}}
</style>
</head>
<body>
<main>
  <header>
    <span class="date">{paivays}</span>
    <h1>{otsikko}</h1>
  </header>

  <section aria-labelledby="saa">
    <div class="head"><h2 id="saa">Sää · {paikka}</h2><span class="src">Ilmatieteen laitos</span></div>
    {saa}
  </section>

  <section aria-labelledby="sahko">
    <div class="head"><h2 id="sahko">Pörssisähkö tänään</h2><span class="src">Tuntikeskiarvot, c/kWh, sis. alv</span></div>
    {sahko}
  </section>

  <section aria-labelledby="nhl">
    <div class="head"><h2 id="nhl">NHL · yön suomalaiset</h2><span class="src">{nhl_pvm} illan ottelut</span></div>
    {nhl}
  </section>

  <p class="foot">Päivitetty {paivitetty}. Lähteet: <a href="https://www.ilmatieteenlaitos.fi/avoin-data">Ilmatieteen laitoksen avoin data</a> ·
  <a href="https://porssisahko.net/">porssisahko.net</a> (Nord Pool) · <a href="https://www.nhl.com/">NHL.com</a>.
  "Tuntuu kuin" on laskettu lämpötilasta ja tuulesta.</p>
</main>
<script>
(function(){{
  var p = window.SAHKO; if(!p) return;
  var svg = document.getElementById("chart"); if(!svg) return;
  var col = function(v){{return v<5?"var(--cheap)":v<15?"var(--mid)":"var(--high)";}};
  var vals = p.map(function(x){{return x[1];}});
  var top = Math.max(10, Math.ceil(Math.max.apply(null, vals)/10)*10);
  var bot = Math.min(0, Math.floor(Math.min.apply(null, vals)));
  var W=720,H=240,L=36,R=8,T=16,B=28,bw=(W-L-R)/p.length;
  var y=function(v){{return T+(H-T-B)*(top-v)/(top-bot);}};
  var step = top<=20?5:top<=60?10:top<=150?25:50, s="";
  for(var g=0; g<=top; g+=step) s+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(g)+'" y2="'+y(g)+'" stroke="var(--line)"/><text x="'+(L-6)+'" y="'+(y(g)+4)+'" text-anchor="end">'+g+'</text>';
  var nowH=-1;
  try{{var d=new Date(new Date().toLocaleString("en-US",{{timeZone:"Europe/Helsinki"}}));
     var iso=d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
     if(iso==="{iso}") nowH=d.getHours();}}catch(e){{}}
  var fmt=function(v){{return v.toFixed(2).replace(".",",").replace("-","−");}};
  var imax=vals.indexOf(Math.max.apply(null,vals)), nowI=-1;
  p.forEach(function(x,i){{
    var h=x[0], v=x[1], X=L+i*bw+2, y0=y(0), yv=y(v);
    s+='<rect x="'+X+'" y="'+Math.min(y0,yv)+'" width="'+(bw-4)+'" height="'+Math.max(Math.abs(y0-yv),1)+'" rx="2" fill="'+col(v)+'"><title>klo '+h+'–'+(h+1)+': '+fmt(v)+' c/kWh</title></rect>';
    if(h%3===0) s+='<text x="'+(X+(bw-4)/2)+'" y="'+(H-10)+'" text-anchor="middle">'+String(h).padStart(2,"0")+'</text>';
    if(h===nowH){{ nowI=i; s+='<rect x="'+(X-2)+'" y="'+T+'" width="'+bw+'" height="'+(y(bot)-T)+'" fill="none" stroke="var(--accent)" stroke-width="2" rx="3"/>'; }}
  }});
  s+='<text x="'+(L+imax*bw+bw/2)+'" y="'+(y(vals[imax])-4)+'" text-anchor="middle" style="fill:var(--fg);font-weight:700">'+fmt(vals[imax]).replace(/,(\\d)\\d$/,",$1")+'</text>';
  svg.innerHTML=s;
  var nv=document.getElementById("nowV"), ns=document.getElementById("nowS");
  if(nowI>=0){{ nv.textContent=fmt(vals[nowI]); nv.style.color=col(vals[nowI]); ns.textContent="c/kWh, klo "+nowH+"–"+(nowH+1); }}
  else {{ nv.textContent="–"; ns.textContent="sivu on eri päivältä"; }}
}})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
