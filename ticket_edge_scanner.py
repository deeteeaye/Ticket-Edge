#!/usr/bin/env python3
import os,json,re,hashlib,urllib.request
from datetime import datetime,timezone

URL=os.environ['SUPABASE_URL'].rstrip('/')
KEY=os.environ['SUPABASE_SERVICE_ROLE_KEY']
H={'apikey':KEY,'Authorization':f'Bearer {KEY}','Content-Type':'application/json','User-Agent':'TicketEdge/11.0 (+zero-cost public-source monitor)'}
KEYWORDS=['presale','pre-sale','on sale','onsale','tickets on sale','added show','second show','new date','venue upgrade','artist presale','venue presale','amex','american express','citi','visa','register','registration','general sale','live nation presale','vip package presale']
CATS=[('ADDED_SHOW',['added show','second show','new date']),('PRESALE',['presale','pre-sale','artist presale','venue presale','amex','american express','citi','visa','live nation presale','vip package presale']),('GENERAL_ONSALE',['general sale','on sale','onsale','tickets on sale']),('REGISTRATION',['register','registration','sign up']),('VENUE_UPGRADE',['venue upgrade'])]

def api(path,method='GET',body=None,headers=None):
    hh=dict(H); hh.update(headers or {})
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(f'{URL}/rest/v1/{path}',data=data,headers=hh,method=method)
    with urllib.request.urlopen(req,timeout=30) as r:
        raw=r.read().decode() or 'null'; return json.loads(raw)

def textify(html):
    html=re.sub(r'(?is)<script.*?>.*?</script>',' ',html); html=re.sub(r'(?is)<style.*?>.*?</style>',' ',html)
    html=re.sub(r'(?s)<[^>]+>',' ',html); return re.sub(r'\s+',' ',html).strip()

def fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':H['User-Agent']})
    with urllib.request.urlopen(req,timeout=25) as r: return r.status,r.read(2_000_000).decode('utf-8','ignore')

def classify(t):
    lo=t.lower(); hits=[k for k in KEYWORDS if k in lo]
    for name,terms in CATS:
        if any(x in lo for x in terms): return hits,name
    return hits,None

def schedules(t):
    p=r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)\s*(?:[A-Z]{2,4})?)'
    out=[]
    for x in re.findall(p,t,re.I):
        x=re.sub(r'\s+',' ',x).strip()
        if x.lower() not in [z.lower() for z in out]: out.append(x)
    return out[:12]

def metadata(t,s):
    hits,cat=classify(t); lo=t.lower(); ss=s['source_name']; event=None
    if '—' in ss: event=ss.split('—',1)[1].strip()
    vm=re.search(r'Venue\s+Details\s+([A-Z][A-Za-z0-9&\'().,\- ]{2,100})',t,re.I); venue=vm.group(1).strip() if vm else None
    lm=re.search(r'([A-Z][A-Za-z .\'-]+),\s*([A-Z]{2})\s+\d{5}',t); city,state=(lm.group(1).strip(),lm.group(2)) if lm else (None,None)
    sch=schedules(t); conf=(25 if cat else 0)+(20 if sch else 0)+(15 if event else 0)+(10 if venue else 0)+(10 if city else 0)+(10 if 'tickets go on sale' in lo else 0)+(10 if 'sign up' in lo or 'registration' in lo else 0)
    card='AMEX' if any(x in hits for x in ['amex','american express']) else 'CITI' if 'citi' in hits else 'VISA' if 'visa' in hits else None
    access='cardmember' if card else 'registration' if any(x in hits for x in ['register','registration']) else 'public/unknown'
    return dict(catalyst_hits=hits,catalyst_type=cat,event_name=event,venue=venue,city=city,state=state,schedule_texts=sch,candidate_confidence=min(conf,100),card_type=card,access_method=access)

def patch_source(i,p): api(f'scanner_sources?id=eq.{i}','PATCH',p,{'Prefer':'return=minimal'})
def upsert(p): api('scanner_candidates?on_conflict=owner_user_id,fingerprint','POST',p,{'Prefer':'resolution=merge-duplicates,return=minimal'})

def main():
    sources=api('scanner_sources?enabled=eq.true&select=id,owner_user_id,source_name,source_url,last_content_hash&order=source_priority.desc')
    print('enabled sources:',len(sources))
    for s in sources:
        now=datetime.now(timezone.utc).isoformat()
        try:
            status,raw=fetch(s['source_url']); txt=textify(raw); digest=hashlib.sha256(txt.encode()).hexdigest(); changed=digest!=(s.get('last_content_hash') or '')
            m=metadata(txt,s); patch={'last_checked_at':now,'last_http_status':status,'last_content_hash':digest,'updated_at':now,'consecutive_failures':0,'last_error':None}
            if changed: patch['last_change_at']=now
            patch_source(s['id'],patch)
            if changed and m['catalyst_hits']:
                lo=txt.lower(); pos=[lo.find(k.lower()) for k in m['catalyst_hits'] if lo.find(k.lower())>=0]; first=min(pos or [0]); excerpt=txt[max(0,first-400):first+1600]
                efp=hashlib.sha256('|'.join([(m.get('event_name') or '').lower(),(m.get('venue') or '').lower(),(m.get('city') or '').lower(),(m.get('state') or '').lower()]).encode()).hexdigest()
                fp=hashlib.sha256(f"{s['id']}|{digest}|{efp}".encode()).hexdigest()
                upsert({'owner_user_id':s['owner_user_id'],'source_id':s['id'],'title':m['event_name'] or f"{s['source_name']}: ticket-drop change",'event_url':s['source_url'],'raw_excerpt':excerpt[:3000],'fingerprint':fp,'status':'NEW','catalyst_type':m['catalyst_type'],'catalyst_hits':m['catalyst_hits'][:20],'event_name':m['event_name'],'venue':m['venue'],'city':m['city'],'state':m['state'],'access_method':m['access_method'],'card_type':m['card_type'],'candidate_confidence':m['candidate_confidence'],'event_fingerprint':efp,'normalized_status':'REVIEW','lifecycle_state':'UNSCHEDULED','last_seen_at':now,'extracted_json':{'schedule_texts':m['schedule_texts']}})
                print('candidate:',s['source_name'],'|',m['catalyst_type'],'| confidence=',m['candidate_confidence'])
            else: print('checked:',s['source_name'],'changed=',changed,'hits=',len(m['catalyst_hits']))
        except Exception as e:
            print('ERROR',s['source_name'],e)
            try: patch_source(s['id'],{'last_checked_at':now,'last_http_status':0,'updated_at':now,'consecutive_failures':1,'last_error':str(e)[:1000]})
            except Exception: pass
    print('maintenance:',api('rpc/ticket_edge_run_maintenance','POST',{}))

if __name__=='__main__': main()
