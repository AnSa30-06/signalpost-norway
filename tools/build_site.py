#!/usr/bin/env python3
"""Render envelopes.jsonl into a static site that opens from file:// with no network.

    python tools/build_site.py --envelopes out/envelopes.jsonl --out site/

Writes site/index.html (directory: search, filters, sortable columns, compare panel) and
site/c/<org>.html (one page per envelope with every claim linked to its evidence).
Stdlib only. No external assets, no CDN, no framework. All HTML is escaped.
`python -m signalpost site` calls build() from this module.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

SECTIONS = ("identity", "accounts", "leadership", "workplaces", "web", "hiring", "activity")
TITLES = {"identity": "Legal identity and public brand", "accounts": "Annual accounts",
          "leadership": "Leadership (registered roles and site leaders)", "workplaces": "Registered workplaces and locations",
          "web": "Verified website and company-owned profiles", "hiring": "Hiring", "activity": "Dated public activity"}
# The summary columns accept any of these claim field names. registry.py decides the exact names.
ALIASES = {
    "employees": ["registry_employees", "employees", "registered_employees", "employee_count"],
    "revenue": ["revenue", "operating_revenue", "driftsinntekter", "sum_driftsinntekter", "turnover"],
    "annual_result": ["annual_result", "aarsresultat", "net_result", "result"],
    "active_jobs": ["active_job_count"],
    "industry": ["industry_label", "industry", "industry_code", "naeringskode", "nace_code"],
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
URL_RE = re.compile(r"^https?://\S+$")


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def json_script(obj) -> str:
    """JSON safe inside a <script> tag: '<' is escaped so '</script>' can never appear."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def fmt_num(n) -> str:
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else esc(n)


def fmt_value(v) -> str:
    if v is None or v == "":
        return "<span class=muted>—</span>"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return fmt_num(v)
    if isinstance(v, dict):
        return "; ".join(f"{esc(k)}: {fmt_value(x)}" for k, x in v.items() if x not in (None, "", []))
    if isinstance(v, list):
        return "<br>".join(fmt_value(x) for x in v)
    s = str(v)
    if URL_RE.match(s):
        return f'<a href="{esc(s)}" rel="noopener">{esc(s)}</a>'
    return esc(s)


def scalar(v):
    """Reduce a claim value to one number for the directory (dict values use value/amount/count)."""
    if isinstance(v, dict):
        for k in ("value", "amount", "count"):
            if k in v:
                return v[k]
        return None
    return v if isinstance(v, (int, float, str)) and not isinstance(v, bool) else None


def pick(claims, names):
    """Newest available claim whose field is one of names."""
    hits = [c for c in claims if c.get("field") in names and c.get("availability") == "available"]
    hits.sort(key=lambda c: (c.get("reporting_period") or "", c.get("effective_date") or ""), reverse=True)
    return hits[0] if hits else None


def latest_date(claims):
    best = None
    for c in claims:
        if c.get("section") not in ("activity", "hiring") or c.get("availability") != "available":
            continue
        v = c.get("value")
        cands = [c.get("effective_date")]
        if isinstance(v, dict):
            cands += [v.get(k) for k in ("date", "date_posted", "lastmod")]
        elif isinstance(v, str):
            cands.append(v)
        for d in cands:
            if isinstance(d, str) and DATE_RE.match(d) and (best is None or d > best):
                best = d
    return best


def summary(env) -> dict:
    ident = env.get("identity") or {}
    claims = env.get("claims") or []
    secs = env.get("sections") or {}
    web = pick(claims, ("official_website",))
    rev = pick(claims, ALIASES["revenue"])

    def num(key):
        c = pick(claims, ALIASES[key])
        return scalar(c.get("value")) if c else None

    return {"org": env.get("organisation_number"), "name": ident.get("legal_name"), "brand": ident.get("public_brand"),
            "form": ident.get("legal_form"), "muni": ident.get("municipality"), "ind": ident.get("industry_label"),
            "emp": num("employees"), "rev": num("revenue"), "period": rev.get("reporting_period") if rev else None,
            "res": num("annual_result"), "jobs": num("active_jobs"),
            "web": web["value"] if web and isinstance(web.get("value"), str) else None,
            "leaders": sum(1 for c in claims if c.get("section") == "leadership" and c.get("availability") == "available"),
            "latest": latest_date(claims), "cov": sum(1 for s in SECTIONS if secs.get(s) == "available"),
            "status": (env.get("run") or {}).get("terminal_status"), "changes": len(env.get("changes") or [])}


# ---- HTML pieces ----------------------------------------------------------------------------------------
CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#5f6368;--line:#d9d9d9;--card:#f3f4f6;--acc:#0b57d0;--ok:#1b7f3b;--warn:#9a5b00;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e6e6e6;--mut:#a3a8b0;--line:#33383f;--card:#1c1f24;--acc:#8ab4f8;--ok:#5dc27a;--warn:#e0a24a;--bad:#f28b82}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{padding:12px;max-width:1200px;margin:0 auto}a{color:var(--acc)}h1{font-size:1.35rem;margin:.4em 0}h2{font-size:1.1rem;margin:1.3em 0 .4em}h3{font-size:1rem;margin:.8em 0 .3em}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}table{border-collapse:collapse;width:100%;min-width:640px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{background:var(--card)}
th[data-k]{cursor:pointer;user-select:none;min-height:40px}
input,select,button{font:inherit;min-height:40px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{cursor:pointer}input[type=checkbox]{min-height:0;width:22px;height:22px;margin:9px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}.bar label{display:flex;align-items:center;gap:2px;min-height:40px}#q{flex:1 1 220px}
.st{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.85em;border:1px solid var(--line);white-space:nowrap}
.st-available{color:var(--ok);border-color:var(--ok)}.st-blocked,.st-failed{color:var(--bad);border-color:var(--bad)}
.st-ambiguous,.st-not_available{color:var(--warn);border-color:var(--warn)}
.muted{color:var(--mut)}.note{font-size:.9em;color:var(--mut)}code{font-size:.9em;word-break:break-all}
.pop{margin-top:6px;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--card);max-width:560px;font-size:.9em;word-break:break-word}
.ev+.ev{border-top:1px solid var(--line);margin-top:6px;padding-top:6px}
blockquote{margin:6px 0 0;padding-left:8px;border-left:3px solid var(--acc);white-space:pre-wrap}
.card{background:var(--card);padding:10px 12px;border-radius:8px;margin:8px 0;word-break:break-word}
.cmp{display:none}.cmp.on{display:block}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;margin:8px 0}dt{color:var(--mut)}dd{margin:0}
pre{overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-size:.85em}
nav{font-size:.9em;margin-bottom:8px}nav a{margin-right:10px}
@media(min-width:700px){main{padding:20px}h1{font-size:1.8rem}}
"""

INDEX_JS = r"""
var D=JSON.parse(document.getElementById('data').textContent),S={q:'',form:'',muni:'',web:false,hire:false,k:'name',dir:1,sel:{}};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function num(n){if(n==null||n==='')return '—';if(typeof n!=='number')return esc(n);return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g,' ')}
function opts(id,key){var el=document.getElementById(id),v={};D.forEach(function(r){if(r[key])v[r[key]]=1});
 Object.keys(v).sort().forEach(function(x){var o=document.createElement('option');o.value=x;o.textContent=x;el.appendChild(o)})}
function cmp(a,b){var x=a[S.k],y=b[S.k];if(x==null&&y==null)return 0;if(x==null)return 1;if(y==null)return -1;
 if(typeof x==='number'&&typeof y==='number')return (x-y)*S.dir;return String(x).localeCompare(String(y))*S.dir}
function rows(){var q=S.q.toLowerCase();return D.filter(function(r){
 if(S.form&&r.form!==S.form)return false;if(S.muni&&r.muni!==S.muni)return false;if(S.web&&!r.web)return false;if(S.hire&&!(r.jobs>0))return false;
 return !q||[r.name,r.brand,r.org,r.muni,r.ind].join(' ').toLowerCase().indexOf(q)>=0}).sort(cmp)}
function render(){var rs=rows(),h='';rs.forEach(function(r){
 h+='<tr><td><input type=checkbox class=sel data-o="'+esc(r.org)+'"'+(S.sel[r.org]?' checked':'')+' aria-label="Compare '+esc(r.name)+'"></td>'
 +'<td><a href="c/'+esc(r.org)+'.html">'+esc(r.name||r.org)+'</a>'+(r.brand&&r.brand!==r.name?'<div class=muted>'+esc(r.brand)+'</div>':'')+'</td>'
 +'<td><code>'+esc(r.org)+'</code></td><td>'+esc(r.form)+'</td><td>'+esc(r.muni)+'</td><td>'+num(r.emp)+'</td><td>'+num(r.rev)+'</td>'
 +'<td>'+num(r.jobs)+'</td><td>'+r.cov+'/7</td><td>'+(r.web?'<a href="'+esc(r.web)+'" rel=noopener>site</a>':'—')+'</td></tr>'});
 document.getElementById('tb').innerHTML=h;document.getElementById('cnt').textContent=rs.length+' of '+D.length+' shown';
 document.querySelectorAll('th[data-k]').forEach(function(t){t.textContent=t.getAttribute('data-l')+(t.getAttribute('data-k')===S.k?(S.dir>0?' ▲':' ▼'):'')});compare()}
var CF=[['name','Name'],['org','Org. number'],['form','Legal form'],['muni','Municipality'],['emp','Employees'],['rev','Revenue'],['period','Revenue period'],
 ['res','Annual result'],['web','Website'],['jobs','Active jobs'],['leaders','Leaders (claims)'],['latest','Latest activity'],['cov','Sections available']];
function compare(){var ids=Object.keys(S.sel).filter(function(k){return S.sel[k]}),p=document.getElementById('cmp');p.className='cmp'+(ids.length?' on':'');if(!ids.length)return;
 var cs=ids.map(function(o){for(var i=0;i<D.length;i++)if(D[i].org===o)return D[i]}).filter(Boolean),h='<tr><th>Field</th>';
 cs.forEach(function(c){h+='<th><a href="c/'+esc(c.org)+'.html">'+esc(c.name||c.org)+'</a></th>'});h+='</tr>';
 CF.forEach(function(f){h+='<tr><td>'+f[1]+'</td>';cs.forEach(function(c){var v=c[f[0]];
  h+='<td>'+(f[0]==='web'?(v?'<a href="'+esc(v)+'" rel=noopener>'+esc(v)+'</a>':'—'):f[0]==='cov'?v+'/7':typeof v==='number'?num(v):esc(v||'—'))+'</td>'});h+='</tr>'});
 document.getElementById('ct').innerHTML=h}
opts('form','form');opts('muni','muni');
document.getElementById('q').addEventListener('input',function(e){S.q=e.target.value;render()});
['form','muni'].forEach(function(id){document.getElementById(id).addEventListener('change',function(e){S[id]=e.target.value;render()})});
document.getElementById('web').addEventListener('change',function(e){S.web=e.target.checked;render()});
document.getElementById('hire').addEventListener('change',function(e){S.hire=e.target.checked;render()});
document.querySelectorAll('th[data-k]').forEach(function(t){t.addEventListener('click',function(){var k=t.getAttribute('data-k');S.dir=(S.k===k)?-S.dir:1;S.k=k;render()})});
document.getElementById('tb').addEventListener('change',function(e){if(e.target.classList.contains('sel')){S.sel[e.target.getAttribute('data-o')]=e.target.checked;compare()}});
document.getElementById('clr').addEventListener('click',function(){S.sel={};render()});
render();
"""

COMPANY_JS = r"""
var E=JSON.parse(document.getElementById('env').textContent),EV={};(E.evidence||[]).forEach(function(e){EV[e.id]=e});
var AL=JSON.parse(document.getElementById('aliases').textContent);
document.getElementById('raw').textContent=JSON.stringify(E,null,1);
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function val(v){if(v==null||v==='')return '<span class=muted>—</span>';if(typeof v==='boolean')return v?'yes':'no';
 if(typeof v==='number')return String(v).replace(/\B(?=(\d{3})+(?!\d))/g,' ');
 if(Array.isArray(v))return v.map(val).join('<br>');
 if(typeof v==='object')return Object.keys(v).filter(function(k){return v[k]!=null&&v[k]!==''}).map(function(k){return esc(k)+': '+val(v[k])}).join('; ');
 var s=String(v);return /^https?:\/\/\S+$/.test(s)?'<a href="'+esc(s)+'" rel=noopener>'+esc(s)+'</a>':esc(s)}
function evs(ids){return (ids||[]).map(function(id){var e=EV[id];if(!e)return '<div class=note>evidence '+esc(id)+' missing</div>';
 return '<div class=ev><a href="'+esc(e.source_url)+'" rel=noopener>'+esc(e.source_url)+'</a><div class=note>'+esc(e.source_class)+' · retrieved '+esc(e.retrieved_at)
 +' · sha256 '+esc((e.content_sha256||'').slice(0,12))+'</div><blockquote>'+esc(e.claim_span)+'</blockquote></div>'}).join('')}
document.addEventListener('click',function(ev){var b=ev.target.closest('.evb');
 if(b){var p=b.nextElementSibling,open=p.hidden;document.querySelectorAll('.pop').forEach(function(x){x.hidden=true});
  document.querySelectorAll('.evb').forEach(function(x){x.setAttribute('aria-expanded','false')});p.hidden=!open;b.setAttribute('aria-expanded',String(open))}
 else if(!ev.target.closest('.pop')){document.querySelectorAll('.pop').forEach(function(x){x.hidden=true})}});
document.addEventListener('keydown',function(ev){if(ev.key==='Escape')document.querySelectorAll('.pop').forEach(function(x){x.hidden=true})});
var ROUTES=[
 {re:/\b(ceo|daglig leder|chief|managing director|general manager|who (runs|leads|manages|is in charge))\b/i,sec:'leadership',pref:/dagl|ceo|chief|managing|general manager/i},
 {re:/\b(board|styre|chair|director|leader|leadership|management|who)\b/i,sec:'leadership'},
 {re:/(revenue|turnover|omsetning|sales|driftsinntekt)/i,fields:['revenue']},
 {re:/(profit|result|earnings|aarsresultat|årsresultat|loss)/i,fields:['annual_result']},
 {re:/\b(employees?|staff|headcount|ansatte|workforce)\b/i,fields:['employees']},
 {re:/(hiring|hire|jobs?\b|vacanc|recruit|stilling|careers?|positions?|open roles?)/i,sec:'hiring'},
 {re:/\b(where|located|location|address|office|municipality|kommune|workplace|adresse|locations?)\b/i,sec:'workplaces',fields:['municipality','business_address','postal_address','registered_address']},
 {re:/\b(website|site|url|domain|homepage|web)\b/i,fields:['official_website']},
 {re:/(social|linkedin|facebook|instagram|youtube|twitter|tiktok)/i,fields:['social_profile']},
 {re:/\b(changed?|changes|since|refresh|diff|different|new)\b/i,changes:true},
 {re:/\b(contact|email|e-post|phone|telefon|tel)\b/i,fields:['contact_email','contact_phone']},
 {re:/(news|activity|recent|latest|update|happen)/i,sec:'activity'},
 {re:/(accounts?\b|financ|filing|regnskap|assets|equity|debt)/i,sec:'accounts'},
 {re:/\b(what does|does|do|industry|business|sector|description|about|brand)\b/i,fields:['industry','website_description','website_title','public_brand']}
];
function ask(q){var r=null,out=document.getElementById('ans');for(var i=0;i<ROUTES.length;i++)if(ROUTES[i].re.test(q)){r=ROUTES[i];break}
 var none='<p><b>The evidence does not establish this.</b></p>';
 if(!r){out.innerHTML=none+'<p class=muted>Try: who is the CEO, revenue, employees, is it hiring, where is it, website, what changed.</p>';return}
 if(r.changes){var ch=E.changes||[];if(!ch.length){var base=((E.run||{}).refresh||{}).baseline;
   out.innerHTML='<p>'+(base?'Baseline run: there is no previous run to compare against.':'No changes were recorded against the previous run.')+'</p>';return}
  out.innerHTML=ch.map(function(c){return '<div class=card><b>'+esc(c.change_type)+'</b> ('+esc(c.materiality)+') '+esc(c.field)+': '+val(c.previous_value)+' → '+val(c.current_value)+evs(c.evidence_ids)+'</div>'}).join('');return}
 var names=(r.fields||[]).reduce(function(a,f){return a.concat(AL[f]||[f])},[]);
 var hits=(E.claims||[]).filter(function(c){return (r.sec&&c.section===r.sec)||names.indexOf(c.field)>=0});
 if(r.pref){var p=hits.filter(function(c){return r.pref.test(JSON.stringify(c.value))});if(p.length)hits=p}
 var ok=hits.filter(function(c){return c.availability==='available'});
 if(!ok.length){out.innerHTML=none+hits.map(function(c){return '<p class=note>'+esc(c.field)+': '+esc(c.availability)+(c.note?' — '+esc(c.note):'')+'</p>'}).join('');return}
 out.innerHTML=ok.slice(0,12).map(function(c){return '<div class=card><b>'+esc(c.field)+'</b>'+(c.reporting_period?' <span class=muted>('+esc(c.reporting_period)+')</span>':'')+': '+val(c.value)+evs(c.evidence_ids)+'</div>'}).join('')}
document.getElementById('ask').addEventListener('submit',function(e){e.preventDefault();ask(document.getElementById('qq').value)});
"""


def page(title: str, body: str, tail: str = "") -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main>{tail}</body></html>")


def evidence_html(evmap: dict, ids: list) -> str:
    if not ids:
        return "<span class=muted>—</span>"
    parts = []
    for eid in ids:
        e = evmap.get(eid)
        if not e:
            parts.append(f"<div class=ev><span class=note>evidence {esc(eid)} missing</span></div>")
            continue
        src = e.get("source_url")
        final = e.get("final_url")
        meta = [esc(e.get("source_class")), f"retrieved {esc(e.get('retrieved_at'))}", f"HTTP {esc(e.get('http_status'))}",
                f"sha256 {esc((e.get('content_sha256') or '')[:12])}", esc(e.get("extraction_method"))]
        if e.get("reporting_period"):
            meta.append(f"period {esc(e['reporting_period'])}")
        parts.append(f'<div class=ev><b>{esc(eid)}</b> <a href="{esc(src)}" rel="noopener">{esc(src)}</a>'
                     + (f"<div class=note>final: {esc(final)}</div>" if final and final != src else "")
                     + f"<div class=note>{' · '.join(meta)}</div>"
                     + (f"<div class=note>snapshot: <code>{esc(e['snapshot_path'])}</code></div>" if e.get("snapshot_path") else "")
                     + f"<blockquote>{esc(e.get('claim_span'))}</blockquote></div>")
    return (f"<button class=evb type=button aria-expanded=false>evidence ({len(ids)})</button>"
            f"<div class=pop hidden>{''.join(parts)}</div>")


def claims_table(claims: list, evmap: dict) -> str:
    if not claims:
        return "<p class=muted>No claims recorded for this section.</p>"
    rows = []
    for c in claims:
        st = esc(c.get("availability"))
        rows.append(f"<tr><td><code>{esc(c.get('field'))}</code></td><td>{fmt_value(c.get('value'))}</td>"
                    f"<td><span class='st st-{st}'>{st}</span></td><td>{esc(c.get('confidence'))}</td>"
                    f"<td>{esc(c.get('reporting_period') or c.get('effective_date') or '')}</td>"
                    f"<td>{evidence_html(evmap, c.get('evidence_ids') or [])}</td><td class=note>{esc(c.get('note') or '')}</td></tr>")
    return ("<div class=wrap><table><thead><tr><th>Field</th><th>Value</th><th>State</th><th>Conf.</th><th>Period / date</th>"
            f"<th>Evidence</th><th>Note</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>")


def simple_table(rows: list, cols: list, evmap: dict | None = None) -> str:
    if not rows:
        return "<p class=muted>None.</p>"
    head = "".join(f"<th>{esc(t)}</th>" for _, t in cols)
    body = []
    for r in rows:
        cells = []
        for key, _ in cols:
            v = r.get(key)
            cells.append(f"<td>{evidence_html(evmap, v or []) if evmap is not None and key.endswith('evidence_ids') else fmt_value(v)}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return f"<div class=wrap><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def company_page(env: dict) -> str:
    ident = env.get("identity") or {}
    run = env.get("run") or {}
    ops = env.get("operations") or {}
    syn = env.get("synthesis") or {}
    secs = env.get("sections") or {}
    claims = env.get("claims") or []
    evmap = {e.get("id"): e for e in env.get("evidence") or []}
    s = summary(env)
    org = env.get("organisation_number") or ""
    name = ident.get("legal_name") or org
    ts = esc(run.get("terminal_status"))
    head = [f"<nav><a href=\"../index.html\">← Directory</a></nav><h1>{esc(name)}</h1><p>"]
    if ident.get("public_brand") and ident["public_brand"] != name:
        head.append(f"Brand: <b>{esc(ident['public_brand'])}</b> · ")
    head.append(f"Org. number <code>{esc(org)}</code> · {esc(ident.get('legal_form'))} · {esc(ident.get('municipality'))}")
    if ident.get("industry_label"):
        head.append(f" · {esc(ident['industry_label'])} ({esc(ident.get('industry_code'))})")
    head.append(f" · run status <span class='st st-{ts}'>{ts}</span></p>")
    head.append(f"<p>Website: <a href=\"{esc(s['web'])}\" rel=\"noopener\">{esc(s['web'])}</a></p>" if s["web"]
                else f"<p>Website: <span class='st st-{esc(secs.get('web'))}'>{esc(secs.get('web') or 'unknown')}</span> (no verified official website is published)</p>")
    parts = ["".join(head), "<h2>Summary (from claims only)</h2>", f"<p>{esc(syn.get('summary') or 'No synthesis available.')}</p>"]
    for key in ("what_it_does", "size", "leadership", "footprint", "hiring", "recent_activity", "what_changed"):
        if syn.get(key):
            parts.append(f"<p><b>{esc(key.replace('_', ' ').capitalize())}:</b> {esc(syn[key])}</p>")
    cannot = syn.get("cannot_establish") or []
    parts.append("<h3>What the evidence cannot establish</h3>")
    parts.append("<ul>" + "".join(f"<li>{esc(x)}</li>" for x in cannot) + "</ul>" if cannot else "<p class=muted>Nothing listed.</p>")
    parts.append("<h2>Ask this profile</h2><form id=ask class=bar><input id=qq type=search placeholder=\"who is the CEO · revenue · is it hiring · where · website · what changed\" "
                 "aria-label=\"Ask this profile\" style=\"flex:1 1 220px\"><button type=submit>Ask</button></form>"
                 "<p class=note>Answers come only from claims on this page, each with its evidence. Nothing is generated.</p><div id=ans></div>")
    parts.append("<h2>Sections</h2><p>" + " ".join(
        f"<a href=\"#sec-{sec}\"><span class='st st-{esc(secs.get(sec))}'>{esc(sec)}: {esc(secs.get(sec) or 'missing')}</span></a>" for sec in SECTIONS) + "</p>")
    for sec in SECTIONS:
        st = esc(secs.get(sec) or "missing")
        parts.append(f"<section id=sec-{sec}><h2>{esc(TITLES[sec])} <span class='st st-{st}'>{st}</span></h2>"
                     + claims_table([c for c in claims if c.get("section") == sec], evmap) + "</section>")
    parts.append("<h2>Changes since previous run</h2>")
    parts.append(f"<p class=note>baseline: {esc((run.get('refresh') or {}).get('baseline'))} · previous run: {esc(run.get('previous_run_id') or '—')}</p>")
    parts.append(simple_table(env.get("changes") or [], [("field", "Field"), ("change_type", "Type"), ("materiality", "Materiality"),
                                                          ("previous_value", "Previous"), ("current_value", "Current"), ("first_observed", "First observed"),
                                                          ("last_observed", "Last observed"), ("evidence_ids", "Evidence"), ("previous_evidence_ids", "Previous evidence")], evmap))
    parts.append("<h2>Errors</h2>")
    parts.append(simple_table(env.get("errors") or [], [("stage", "Stage"), ("source_url", "Source"), ("message", "Message"), ("availability", "State")]))
    parts.append("<h2>Operations</h2><dl>" + "".join(
        f"<dt>{esc(k)}</dt><dd>{fmt_value(v)}</dd>" for k, v in [("run_id", run.get("run_id")), ("started_at", run.get("started_at")),
                                                                ("completed_at", run.get("completed_at")), ("agent_version", run.get("agent_version")),
                                                                ("schema_version", env.get("schema_version"))] + list(ops.items())) + "</dl>")
    parts.append("<details><summary>Raw envelope JSON</summary><pre id=raw></pre></details><p><a href=\"../index.html\">← Directory</a></p>")
    tail = (f"<script id=env type=application/json>{json_script(env)}</script>"
            f"<script id=aliases type=application/json>{json_script(ALIASES)}</script><script>{COMPANY_JS}</script>")
    return page(f"{name} ({org}) — Signalpost", "".join(parts), tail)


def index_page(rows: list, run_ids: set) -> str:
    body = (f"<h1>Signalpost — company directory</h1><p class=muted>{len(rows)} profiles · run {esc(', '.join(sorted(r for r in run_ids if r)))} · "
            "every value on a company page links to the snapshot and text span that supports it.</p>"
            "<div class=bar><input id=q type=search placeholder=\"Search name, org number, municipality, industry\" aria-label=\"Search\">"
            "<select id=form aria-label=\"Legal form\"><option value=\"\">All legal forms</option></select>"
            "<select id=muni aria-label=\"Municipality\"><option value=\"\">All municipalities</option></select>"
            "<label><input type=checkbox id=web> Verified website</label><label><input type=checkbox id=hire> Hiring</label><span id=cnt class=muted></span></div>"
            "<div id=cmp class=cmp><div class=card><div class=bar><b>Compare</b><button id=clr type=button>Clear</button></div><div class=wrap><table id=ct></table></div></div></div>"
            "<div class=wrap><table><thead><tr><th>Compare</th><th data-k=name data-l=Name>Name</th><th>Org. number</th><th>Form</th>"
            "<th data-k=muni data-l=Municipality>Municipality</th><th data-k=emp data-l=Employees>Employees</th><th data-k=rev data-l=Revenue>Revenue</th>"
            "<th data-k=jobs data-l=\"Active jobs\">Active jobs</th><th data-k=cov data-l=Coverage>Coverage</th><th>Website</th></tr></thead><tbody id=tb></tbody></table></div>"
            "<p class=note>Coverage = sections in state <code>available</code> out of 7. Employees and revenue come from Brønnøysundregistrene claims; "
            "a dash means the claim is not available, not zero.</p>")
    tail = f"<script id=data type=application/json>{json_script(rows)}</script><script>{INDEX_JS}</script>"
    return page("Signalpost — company directory", body, tail)


def build(envelopes: Path, out: Path) -> dict:
    out = Path(out)
    (out / "c").mkdir(parents=True, exist_ok=True)
    rows, run_ids, seen = [], set(), {}
    with open(envelopes, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            env = json.loads(line)
            org = str(env.get("organisation_number") or "")
            if not re.fullmatch(r"\d{9}", org):
                print(f"skip: bad organisation_number {org!r}", file=sys.stderr)
                continue
            seen[org] = env  # last envelope for an org wins
    for org, env in seen.items():
        (out / "c" / f"{org}.html").write_text(company_page(env), encoding="utf-8")
        rows.append(summary(env))
        run_ids.add((env.get("run") or {}).get("run_id"))
    rows.sort(key=lambda r: (r.get("name") or "", r["org"]))
    (out / "index.html").write_text(index_page(rows, run_ids), encoding="utf-8")
    return {"companies": len(rows), "index": str(out / "index.html")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the static Signalpost site from envelopes.jsonl")
    ap.add_argument("--envelopes", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    r = build(a.envelopes, a.out)
    print(f"wrote {r['companies']} company pages and {r['index']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
