"""Rough local simulator (NOT the official scorer): 15 hunting house-style bots vs our bot.
Usage (from the repo root): python3 tools/sim.py bot 20 1     # module, matches, first seed
Only useful to compare versions against each other; real scores come from the Buildathon page."""
import gzip, json, math, os, random, sys, time, importlib
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import navmap

lines = gzip.open(os.path.join(ROOT,'check','stream.jsonl.gz'),'rt').read().splitlines()
HELLO = json.loads(lines[0]); HELLO['map']['dir'] = os.path.join(ROOT,'arena')
CFG = HELLO['config']
NAV = navmap.NavMap(os.path.join(ROOT,'arena'))
PH = CFG['zone']['phases']
DT = 0.1
HUNT = True
import os
HUNT = os.environ.get('HUNT','1')=='1'
TAU = 2*math.pi
def angdiff(a,b): return (a-b+math.pi)%TAU-math.pi

def zone_at(t):
    c=list(CFG['zone']['start_center']); r=CFG['zone']['start_radius']; tt=0.0
    cur=(c[0],c[1],r)
    rng=random.Random(99)  # deterministic next centers inside current
    for k,p in enumerate(PH):
        a=rng.uniform(0,TAU); rr=rng.uniform(0,max(0,cur[2]-p['radius'])*0.8)
        nc=(cur[0]+rr*math.cos(a), cur[1]+rr*math.sin(a))
        s0=tt+p['hold_s']; s1=s0+p['shrink_s']
        if t<s1:
            f=0 if t<s0 else (t-s0)/p['shrink_s']
            cx=cur[0]+(nc[0]-cur[0])*f; cy=cur[1]+(nc[1]-cur[1])*f; r=cur[2]+(p['radius']-cur[2])*f
            return {'center':[cx,cy],'radius':r,'next_center':list(nc),'next_radius':p['radius'],'shrink_start':s0,'shrink_end':s1,'dps':p['dps']}
        cur=(nc[0],nc[1],p['radius']); tt=s1
    return {'center':[cur[0],cur[1]],'radius':0,'next_center':[cur[0],cur[1]],'next_radius':0,'shrink_start':tt,'shrink_end':tt,'dps':260}

class Mech:
    def __init__(s,i,x,y):
        s.id=i; s.x=x; s.y=y; s.z=0.0; s.vx=s.vy=0.0; s.aim=0.0; s.hp=1400.0; s.alive=True
        s.cool=0.0; s.last_dmg=-9; s.ammo=12; s.seen={}  # id->(x,y,t)
        s.events=[]; s.energy=100.0; s.dead_t=None; s.walls=0

def free(x,y): return not NAV.blocked(x,y)

def spawn(rng):
    pts=[]
    c=CFG['zone']['start_center']
    while len(pts)<16:
        a=rng.uniform(0,TAU); r=560*0.8*math.sqrt(rng.uniform(0.2,1))
        x=c[0]+r*math.cos(a); y=c[1]+r*math.sin(a)
        if not (NAV.minX+10<x<NAV.maxX-10 and NAV.minY+10<y<NAV.maxY-10): continue
        if not NAV.nav_free(x,y) or NAV.blocked(x,y): continue
        if any(math.hypot(x-p[0],y-p[1])<130 for p in pts): continue
        pts.append((x,y))
    return pts

def move(m,dx,dy,speed):
    n=math.hypot(dx,dy)
    if n>1: dx/=n; dy/=n
    tvx,tvy=dx*speed,dy*speed
    k=min(1.0,58.5*DT/ max(1.0,math.hypot(tvx-m.vx,tvy-m.vy)))
    m.vx+= (tvx-m.vx)*k; m.vy+=(tvy-m.vy)*k
    nx,ny=m.x+m.vx*DT, m.y+m.vy*DT
    if free(nx,ny): m.x,m.y=nx,ny
    elif free(nx,m.y): m.x=nx; m.vy=0
    elif free(m.x,ny): m.y=ny; m.vx=0
    else: m.vx=m.vy=0; m.walls+=1

def visible(a,b):
    d=math.hypot(b.x-a.x,b.y-a.y)
    if d>200: return False
    if not NAV.sight_clear(a.x,a.y,b.x,b.y): return False
    if d<=45: return True
    return abs(angdiff(math.atan2(b.y-a.y,b.x-a.x),a.aim))<math.radians(60)

def hurt(target,dmg,src,wpn,t,rng):
    # rear armour
    bearing=math.atan2(src.y-target.y,src.x-target.x)
    rear=abs(angdiff(bearing,target.aim))>math.pi/2
    if rear: dmg*=1.5
    target.hp-=dmg; target.last_dmg=t
    target.dmg_taken=getattr(target,'dmg_taken',0)+dmg
    target.events.append({'type':'damaged','amount':dmg,'weapon':wpn,'bearing':bearing,'by':src.id,'rear':rear})

def hit_prob(shooter,target,d):
    sp=math.hypot(target.vx,target.vy)
    return max(0.08,min(0.9,0.85-d/330.0-0.22*sp/11.7))

def run_match(make_ours, seed, verbose=False):
    rng=random.Random(seed)
    sp=spawn(rng); mechs=[Mech(i,*sp[i]) for i in range(16)]
    ours=1+0  # slot of ours
    hello=json.loads(json.dumps(HELLO)); hello['slot']=ours
    bot=make_ours(hello)
    t=0.0; placements=[]; tick=0
    alive_n=16
    house_goal={}
    for m in mechs:
        m.aim=rng.uniform(-3.14,3.14)
    while t<480 and alive_n>1:
        z=zone_at(t)
        shared={}   # house vision shared: id->(x,y)
        for m in mechs:
            if m.id==ours or not m.alive: continue
            for o in mechs:
                if o.id!=m.id and o.alive and visible(m,o): shared[o.id]=(o.x,o.y)
        pending=[]  # (shooter, target, dmg, wpn)
        # ---- house bots
        for m in mechs:
            if m.id==ours or not m.alive: continue
            m.cool=max(0,m.cool-DT)
            cands=[(math.hypot(o.x-m.x,o.y-m.y),o) for o in mechs if o.alive and o.id!=m.id and o.id in shared and math.hypot(o.x-m.x,o.y-m.y)<170]
            cands.sort(key=lambda c:c[0])
            zx,zy=z['next_center'] if z['next_radius']<z['radius'] else z['center']
            gx,gy=zx,zy
            if m.id not in house_goal or rng.random()<0.01:
                a=rng.uniform(0,TAU); r=max(5,z['next_radius']*0.6)*math.sqrt(rng.random())
                house_goal[m.id]=(r*math.cos(a),r*math.sin(a))
            gx+=house_goal[m.id][0]; gy+=house_goal[m.id][1]
            far=[(math.hypot(o.x-m.x,o.y-m.y),o) for o in mechs if o.alive and o.id!=m.id and o.id in shared]
            far.sort(key=lambda c:c[0])
            if HUNT and not cands and far and far[0][0]<450:
                d,o=far[0]
                ang=math.atan2(o.y-m.y,o.x-m.x)
                m.aim+=max(-0.4,min(0.4,angdiff(ang,m.aim)))
                move(m,math.cos(ang),math.sin(ang),11.7)
            elif cands:
                d,o=cands[0]
                ang=math.atan2(o.y-m.y,o.x-m.x)
                # turn toward
                dd=angdiff(ang,m.aim); m.aim+=max(-0.4,min(0.4,dd))
                strafe=1 if (int(t*1.3)+m.id)%2 else -1
                if d>75: dx,dy=math.cos(ang),math.sin(ang)
                elif d<40: dx,dy=-math.cos(ang),-math.sin(ang)
                else: dx,dy=0.0,0.0
                dx+=-math.sin(ang)*0.6*strafe; dy+=math.cos(ang)*0.6*strafe
                move(m,dx,dy,11.7)
                if abs(angdiff(ang,m.aim))<0.12 and m.cool<=0 and NAV.sight_clear(m.x,m.y,o.x,o.y) and d<200:
                    m.cool=0.24
                    if rng.random()<hit_prob(m,o,d): pending.append((m,o,45.0,'gun'))
            else:
                dx,dy=gx-m.x,gy-m.y
                if math.hypot(dx,dy)<6: dx=dy=0
                ang=math.atan2(dy,dx) if (dx or dy) else m.aim
                m.aim+=max(-0.4,min(0.4,angdiff(ang,m.aim)))
                move(m,dx,dy,11.7)
        # ---- our bot
        me=mechs[ours]
        if me.alive:
            vis=[]
            for o in mechs:
                if o.id==ours or not o.alive: continue
                d=math.hypot(o.x-me.x,o.y-me.y)
                if visible(me,o):
                    me.seen[o.id]=(o.x,o.y,t)
                    vis.append(o)
                elif o.id in me.seen and t-me.seen[o.id][2]<5.0:
                    vis.append(o)
            gz=NAV.ground(me.x,me.y)
            obs={'type':'obs','tick':tick,'t':round(t,2),'alive':alive_n,
                 'self':{'id':ours,'pos':[me.x,me.y,gz],'vel':[me.vx,me.vy,0],'aim':[me.aim,0.0],'hp':me.hp,'max_hp':1400,'energy':me.energy,'max_energy':100,'grounded':True,'cubes':0,'upgrades':[],
                         'weapons':[{'kind':'gun','cooldown':me.cool,'ammo':me.ammo,'reload':0,'stats':CFG['weapons']['gun']},None],'in_zone':math.hypot(me.x-z['center'][0],me.y-z['center'][1])<=z['radius'],'stats':CFG['mech']},
                 'mechs':[{'id':o.id,'kind':'mech','pos':[o.x,o.y,NAV.ground(o.x,o.y)],'vel':[o.vx,o.vy,0],'aim':[o.aim,0],'hp':o.hp,'max_hp':1400,'cubes':0,'weapons':['gun'],'charging':False} for o in vis],
                 'projectiles':[],'crates':[],'cubes':[],'pickups':[],
                 'sounds':[{'kind':'gun','bearing':math.atan2(o.y-me.y,o.x-me.x)+rng.gauss(0,0.12),'dist':'near' if math.hypot(o.x-me.x,o.y-me.y)<60 else ('mid' if math.hypot(o.x-me.x,o.y-me.y)<160 else 'far'),'age':0.1} for o in mechs if o.alive and o.id!=ours and o.cool>0.2 and math.hypot(o.x-me.x,o.y-me.y)<320],
                 'beams':[],'events':me.events,'zone':z,'offer':None,'drops':[]}
            me.events=[]
            a=bot.act(obs)
            mv=a.get('move',[0,0]); boost=a.get('boost',False) and me.energy>2
            sp_=19.5 if boost else 11.7
            if boost: me.energy=max(0,me.energy-17*DT)
            else: me.energy=min(100,me.energy+18*DT)
            if 'aim' in a:
                me.aim+=max(-0.4,min(0.4,angdiff(a['aim'][0],me.aim)))
            move(me,mv[0],mv[1],sp_)
            me.cool=max(0,me.cool-DT)
            f=a.get('fire',[False,False])
            if f[0] and me.cool<=0 and me.ammo>0:
                me.cool=0.24; me.ammo-=1
                if me.ammo==0: me.cool=2.4; me.ammo=12
                # nearest enemy within aim cone and LOS
                best=None
                for o in mechs:
                    if o.id==ours or not o.alive: continue
                    d=math.hypot(o.x-me.x,o.y-me.y)
                    if d<230 and abs(angdiff(math.atan2(o.y-me.y,o.x-me.x),me.aim))<math.atan2(2.6,d)+0.02 and NAV.sight_clear(me.x,me.y,o.x,o.y):
                        if best is None or d<best[0]: best=(d,o)
                if best and rng.random()<hit_prob(me,best[1],best[0]): pending.append((me,best[1],45.0,'gun'))
        for sh,tg,dmg,w in pending:
            if tg.alive: hurt(tg,dmg,sh,w,t,rng)
        # ---- zone + regen + deaths
        newly=[]
        for m in mechs:
            if not m.alive: continue
            if math.hypot(m.x-z['center'][0],m.y-z['center'][1])>z['radius']:
                m.hp-=z['dps']*DT; m.last_dmg=t; m.zone_dmg=getattr(m,'zone_dmg',0)+z['dps']*DT
            elif t-m.last_dmg>5: m.hp=min(1400,m.hp+45*DT)
            if m.hp<=0: newly.append(m)
        for m in newly:
            m.alive=False; m.dead_t=t
        if newly:
            place=alive_n  # same-tick deaths share the place
            for m in newly: placements.append((m.id,place))
            alive_n-=len(newly)
        t+=DT; tick+=2
    if alive_n>=1:
        for m in mechs:
            if m.alive:
                placements.append((m.id, 1 if alive_n==1 else sorted([x for x in mechs if x.alive],key=lambda q:-q.hp).index(m)+1))
    pl=dict(placements)
    return 16-pl.get(ours,16), pl.get(ours,16), round(me.dead_t if me.dead_t else t,1), (int(getattr(me,'dmg_taken',0)), int(getattr(me,'zone_dmg',0)))

if __name__=='__main__':
    modname=sys.argv[1]; n=int(sys.argv[2]); s0=int(sys.argv[3]) if len(sys.argv)>3 else 1
    mod=importlib.import_module(modname)
    tot=0; res=[]
    for s in range(s0,s0+n):
        t0=time.time()
        sc,pl,tt,walls=run_match(mod.Bot, s)
        tot+=sc; res.append(pl)
        print(f'seed {s}: place {pl} score {sc} end_t {tt}s dmg(enemy,zone) {walls} ({time.time()-t0:.0f}s wall)', flush=True)
    print('MEAN SCORE', round(tot/n,2), 'places', res)
