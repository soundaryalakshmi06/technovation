import { useState, type ReactNode } from 'react'
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Shield, Eye, EyeOff, Plus, Play, Pause, Square, RotateCcw, Maximize, Upload, VideoOff, CheckCheck, Download, RefreshCw, WifiOff, Lock, FileQuestion, ServerCrash, Cpu } from 'lucide-react'
import { useAuth, home } from './auth'
import { alerts, collisions, events, matches, medicalAssessments, players, session, system, teams, tracks, users, videos } from './api/services'
import type { AlertDto, CollisionDto, EventDto, MatchDto, MeDto, MedicalAssessmentDto, PasswordChangeBody, PlayerDto, PlayerTrackDto, TeamDto, TeamSummaryDto } from './api/dto'
import { isConnected, isDevPreviewEnabled, normalizeApiError } from './api/client'
import { alertPlayerLabel, collisionParticipantPlayer, collisionParticipants, eventPlayer, hasReliablePlayerIdentity, playerIdentityLabel, playerLabel, safeAlertText } from './api/mappers'
import { isWithinDateRange, sortRecent, summarizeIncidents, timestampValue } from './reporting'
import type { Role } from './types'
import { Badge, Card, Confirm, Disclaimer, EmptyState, ErrorState, Modal, Na, PageHeader, RiskBadge, Skeleton, btn, btnP, btnS, inp, useToast } from './ui'
const NC='Backend not connected. Nothing was saved.'
const Field=({label,err,children}:{label:string;err?:string;children:ReactNode})=><label className="block text-sm"><span className="mb-1 block font-medium">{label}</span>{children}{err&&<span role="alert" className="mt-1 block text-xs text-red-600">{err}</span>}</label>

/* ---------- AUTH ---------- */
const ls=z.object({username:z.string().min(1,'Username or email is required'),password:z.string().min(1,'Password is required')})
export function Login(){
  const {login,devPreview,user}=useAuth(); const nav=useNavigate()
  const [show,setShow]=useState(false); const [err,setErr]=useState(''); const [busy,setBusy]=useState(false); const [forgot,setForgot]=useState(false)
  const f=useForm<z.infer<typeof ls>>({resolver:zodResolver(ls)})
  if(user)return <Navigate to={home[user.role]} replace/>
  const go=f.handleSubmit(async v=>{setBusy(true);setErr('');try{await login(v.username,v.password)}catch(e){setErr(normalizeApiError(e))}finally{setBusy(false)}})
  return <div className="grid min-h-screen place-items-center bg-[#0b1f3a] p-4"><div className="w-full max-w-md rounded-2xl bg-white p-8 shadow-xl">
    <div className="mb-6 text-center"><Shield className="mx-auto text-sky-600" size={40}/><h1 className="mt-2 text-2xl font-bold">AthleteGuard</h1><p className="text-sm text-slate-500">AI-Powered Football Injury Detection & Medical Decision Support</p></div>
    {forgot?<div className="space-y-3"><p className="text-sm text-slate-700">For security, password changes require signing in with your current password. After signing in, open Settings → Security. If you cannot sign in, contact your system administrator; unauthenticated email reset is not configured.</p><button className="w-full text-sm text-blue-700 underline" onClick={()=>setForgot(false)}>Back to login</button></div>
    :<form onSubmit={go} className="space-y-4" noValidate>
      {err&&<div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-800">{err}</div>}
      <Field label="Username or email" err={f.formState.errors.username?.message}><input className={inp} autoComplete="username" {...f.register('username')}/></Field>
      <Field label="Password" err={f.formState.errors.password?.message}><div className="relative"><input className={inp} type={show?'text':'password'} autoComplete="current-password" {...f.register('password')}/><button type="button" aria-label={show?'Hide password':'Show password'} className="absolute right-3 top-2.5 text-slate-500" onClick={()=>setShow(s=>!s)}>{show?<EyeOff size={16}/>:<Eye size={16}/>}</button></div></Field>
      <button className={btnP+' w-full justify-center'} disabled={busy}>{busy?'Signing in…':'Sign in'}</button>
      <button type="button" className="w-full text-sm text-blue-700 underline" onClick={()=>setForgot(true)}>Forgot password?</button></form>}
    {isDevPreviewEnabled&&<div className="mt-6 border-t pt-4"><p className="mb-2 flex items-center gap-2 text-xs font-semibold text-amber-800"><Badge tone="amber">DEV</Badge>Developer preview (no backend, no data)</p><div className="grid grid-cols-3 gap-2">{(['ADMIN','MEDICAL','COACH'] as Role[]).map(r=><button key={r} className={btnS+' justify-center'} onClick={()=>{devPreview(r);nav(home[r])}}>{r.charAt(0)+r.slice(1).toLowerCase()}</button>)}</div></div>}
  </div></div>
}

/* ---------- DASHBOARDS ---------- */
const QA:Record<Role,[string,string][]>={
  ADMIN:[['Add team','/teams'],['Register player','/players'],['Create match','/matches'],['System status','/admin/system']],
  COACH:[['My team','/teams'],['View matches','/matches'],['View alerts','/alerts']],
  MEDICAL:[['Review queue','/events'],['Collisions','/collisions'],['Reports','/reports']],
  PLAYER:[['My profile','/players/me'],['My matches','/matches'],['My incidents','/events']],
}
const riskPriority:Record<string,number>={CRITICAL:0,HIGH:1,MEDIUM:2,LOW:3}
function collisionPlayerLabel(collision:CollisionDto):string{
  const participants=collisionParticipants(collision)
  if(!participants)return 'Player'
  const names=participants.map(participant=>playerIdentityLabel(collisionParticipantPlayer(participant))).filter(name=>name!=='Player')
  return names.length?names.join(' · '):'Player'
}
function matchLabel(match:Pick<MatchDto,'name'|'date'>|null|undefined):string{
 const name=match?.name?.trim()
 if(name)return name
 return match?.date?`Match on ${new Date(match.date).toLocaleDateString()}`:'Match'
}
function eventMatchLabel(name:string|null,matchId:number|null,matchNames:Map<number,string>):string{
 if(name?.trim()&&!/^match\s*#\d+$/i.test(name.trim()))return name
 return matchId===null?'—':matchNames.get(matchId)??'Match'
}
type DashboardIncident={id:number;kind:string;player:string;risk:string|null;confidence:number|null;timestamp:string|null;href:string;frame:number|null;videoTime:number|null}
function dashboardIncidents(eventsFound:EventDto[],collisionsFound:CollisionDto[]):DashboardIncident[]{
  const eventRows=eventsFound.map(event=>({
    id:event.id,kind:event.event_type,player:playerIdentityLabel(eventPlayer(event)),risk:event.risk,
    confidence:event.confidence,timestamp:event.timestamp,
    href:`/events/${event.id}`,frame:event.frame_number,videoTime:event.video_time_sec,
  }))
  const collisionRows=collisionsFound.map(collision=>({
    id:collision.id,kind:collision.event_type,player:collisionPlayerLabel(collision),risk:collision.risk,
    confidence:collision.confidence,timestamp:collision.timestamp,href:`/collisions/${collision.id}`,frame:collision.frame_number,
    videoTime:collision.video_time_sec,
  }))
  return sortRecent([...eventRows,...collisionRows])
}
export function Dashboard({role}:{role:Role}){
  const {user}=useAuth()
  const enabled=isConnected&&!!user&&!user.dev
  const eventQuery=useQuery({queryKey:['dashboard','events'],queryFn:()=>events.listAll(),enabled})
  const collisionQuery=useQuery({queryKey:['dashboard','collisions'],queryFn:()=>collisions.listAll(),enabled})
  const matchQuery=useQuery({queryKey:['dashboard','matches'],queryFn:()=>matches.list(),enabled})
  const videoQuery=useQuery({queryKey:['dashboard','videos'],queryFn:()=>videos.listAll(),enabled:enabled&&role!=='PLAYER'})
  const unreadQuery=useQuery({
    queryKey:['alerts','unread-count'],
    queryFn:()=>alerts.listAll({unread:true}),
    enabled,
  })
  const recentAlertQuery=useQuery({
    queryKey:['alerts','dashboard-recent'],
    queryFn:()=>alerts.list({limit:5,offset:0}),
    enabled,
    refetchInterval:30000,
  })
  const alertSubjects=useQueries({queries:(recentAlertQuery.data??[]).map(alert=>({
    queryKey:alert.injury_event_id!==null?['alert-identity','event',alert.injury_event_id]:['alert-identity','collision',alert.collision_event_id],
    queryFn:()=>alert.injury_event_id!==null?events.get(alert.injury_event_id):collisions.get(alert.collision_event_id!),
    enabled:alert.injury_event_id!==null||alert.collision_event_id!==null,
  }))})
  const dashboardAlerts=(recentAlertQuery.data??[]).map((alert,index)=>({alert,subject:alertSubjects[index]?.data})).sort((a,b)=>
    (riskPriority[a.alert.severity?.toUpperCase()??'']??4)-(riskPriority[b.alert.severity?.toUpperCase()??'']??4)
    ||timestampValue(b.alert.created_at)-timestampValue(a.alert.created_at)).slice(0,5)
  const eventRows=eventQuery.data??[]
  const collisionRows=collisionQuery.data??[]
  const collisionIds=new Set(collisionRows.map(collision=>collision.id))
  const allIncidents=dashboardIncidents(eventRows,collisionRows)
  const summary=summarizeIncidents(eventRows,collisionRows)
  const highRisk=allIncidents
    .filter(row=>row.risk?.toUpperCase()==='CRITICAL'||row.risk?.toUpperCase()==='HIGH')
    .sort((a,b)=>(riskPriority[a.risk?.toUpperCase()??'']??4)-(riskPriority[b.risk?.toUpperCase()??'']??4)
      ||timestampValue(b.timestamp)-timestampValue(a.timestamp))
  const playerIncidentCounts=new Map<string,number>()
  const countedPlayerIncidents=new Set<string>()
  const countPlayerIncident=(incidentKey:string,playerId:number|null,name:string)=>{
    if(playerId===null||name==='Player')return
    const key=`${incidentKey}:${playerId}`
    if(countedPlayerIncidents.has(key))return
    countedPlayerIncidents.add(key)
    playerIncidentCounts.set(name,(playerIncidentCounts.get(name)??0)+1)
  }
  for(const event of eventRows){
    const player=eventPlayer(event)
    countPlayerIncident(event.collision_event_id!==null&&collisionIds.has(event.collision_event_id)
      ?`collision:${event.collision_event_id}`:`event:${event.id}`,player.playerId,player.name)
  }
  for(const collision of collisionRows){
    const participants=collisionParticipants(collision)
    for(const participant of participants??[]){
      const player=collisionParticipantPlayer(participant)
      countPlayerIncident(`collision:${collision.id}`,player.playerId,player.name)
    }
  }
  const playerIncidents=[...playerIncidentCounts.entries()].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).slice(0,5)
  const metric=(label:string,query:{isLoading:boolean;isError:boolean},value:number|undefined)=>(
    <Card key={label}><p className="text-xs text-slate-500">{label}</p>{query.isLoading?<Skeleton className="mt-2 h-8 w-12"/>:
      query.isError||value===undefined?<p className="mt-2 text-sm text-slate-500">Unavailable</p>:
      <p className="mt-1 text-3xl font-bold">{value}</p>}</Card>
  )
  const metrics=[
    metric('Total matches',matchQuery,matchQuery.data?.length),
    metric('Active matches',matchQuery,matchQuery.data?.filter(match=>match.status==='PROCESSING').length),
    ...(role==='PLAYER'?[]:[metric('Videos',videoQuery,videoQuery.data?.length)]),
    metric('Total incidents',eventQuery.isError||collisionQuery.isError?{isLoading:false,isError:true}:{
      isLoading:eventQuery.isLoading||collisionQuery.isLoading,isError:false,
    },eventQuery.data&&collisionQuery.data?summary.total:undefined),
    metric('CRITICAL',eventQuery.isError||collisionQuery.isError?{isLoading:false,isError:true}:{
      isLoading:eventQuery.isLoading||collisionQuery.isLoading,isError:false,
    },eventQuery.data&&collisionQuery.data?summary.byRisk.CRITICAL:undefined),
    metric('HIGH',eventQuery.isError||collisionQuery.isError?{isLoading:false,isError:true}:{
      isLoading:eventQuery.isLoading||collisionQuery.isLoading,isError:false,
    },eventQuery.data&&collisionQuery.data?summary.byRisk.HIGH:undefined),
    metric('MEDIUM',eventQuery.isError||collisionQuery.isError?{isLoading:false,isError:true}:{
      isLoading:eventQuery.isLoading||collisionQuery.isLoading,isError:false,
    },eventQuery.data&&collisionQuery.data?summary.byRisk.MEDIUM:undefined),
    metric('LOW',eventQuery.isError||collisionQuery.isError?{isLoading:false,isError:true}:{
      isLoading:eventQuery.isLoading||collisionQuery.isLoading,isError:false,
    },eventQuery.data&&collisionQuery.data?summary.byRisk.LOW:undefined),
    metric('Collisions',collisionQuery,collisionRows.length),
    metric('Unread alerts',unreadQuery,unreadQuery.data?.length),
    metric('Available clips',eventQuery.isError||collisionQuery.isError?{isLoading:false,isError:true}:{
      isLoading:eventQuery.isLoading||collisionQuery.isLoading,isError:false,
    },eventQuery.data&&collisionQuery.data?summary.clips:undefined),
  ]
  return <><PageHeader back={false} title={`${role.charAt(0)+role.slice(1).toLowerCase()} dashboard`} crumbs={[{label:'Dashboard'}]}/>
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">{metrics}</div>
    {!enabled&&<p className="mt-2 text-xs text-slate-500">Live statistics require a connected backend session.</p>}
    <div className="mt-5 grid gap-4 lg:grid-cols-2">
      <Card><div className="flex items-center justify-between gap-3"><h2 className="font-semibold">Recent injury incidents</h2><Link className="text-sm text-blue-700 underline" to="/events">All events</Link></div>
        {eventQuery.isLoading?<Skeleton className="mt-3"/>:eventQuery.isError?<ErrorState title="Unable to load incidents" text={normalizeApiError(eventQuery.error)} onRetry={()=>eventQuery.refetch()}/>:sortRecent(eventRows).slice(0,5).length?
          <div className="mt-3 divide-y">{sortRecent(eventRows).slice(0,5).map(event=><Link key={event.id} to={`/events/${event.id}`} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm hover:text-blue-700"><span><span className="block">{playerIdentityLabel(eventPlayer(event))} · {event.event_type}</span><span className="text-xs text-slate-500">{event.timestamp?new Date(event.timestamp).toLocaleString():'Time not recorded'} · Confidence {event.confidence===null?'—':`${Math.round(event.confidence*100)}%`}</span></span><RiskBadge level={event.risk}/></Link>)}</div>:
          <EmptyState title="No incidents" text="No injury or fall event records are available."/>}
      </Card>
      <Card><div className="flex items-center justify-between gap-3"><h2 className="font-semibold">Recent collisions</h2><Link className="text-sm text-blue-700 underline" to="/collisions">All collisions</Link></div>
        {collisionQuery.isLoading?<Skeleton className="mt-3"/>:collisionQuery.isError?<ErrorState title="Unable to load collisions" text={normalizeApiError(collisionQuery.error)} onRetry={()=>collisionQuery.refetch()}/>:sortRecent(collisionRows).slice(0,5).length?
          <div className="mt-3 divide-y">{sortRecent(collisionRows).slice(0,5).map(collision=><Link key={collision.id} to={`/collisions/${collision.id}`} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm hover:text-blue-700"><span><span className="block">{collisionPlayerLabel(collision)} · Collision</span><span className="text-xs text-slate-500">{collision.timestamp?new Date(collision.timestamp).toLocaleString():'Time not recorded'} · Confidence {collision.confidence===null?'—':`${Math.round(collision.confidence*100)}%`}</span></span><RiskBadge level={collision.risk}/></Link>)}</div>:
          <EmptyState title="No collisions" text="No collision records are available."/>}
      </Card>
      <Card><h2 className="font-semibold">Recent high-risk incidents</h2>{eventQuery.isError||collisionQuery.isError?
        <ErrorState title="Unable to load high-risk incidents" text={normalizeApiError(eventQuery.error??collisionQuery.error)} onRetry={()=>{void eventQuery.refetch();void collisionQuery.refetch()}}/>:
        eventQuery.isLoading||collisionQuery.isLoading?<Skeleton className="mt-3"/>:highRisk.length?
        <div className="mt-3 divide-y">{highRisk.slice(0,5).map(row=><Link key={`${row.href}-${row.id}`} to={row.href} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"><span>{row.player} — {row.risk}</span><RiskBadge level={row.risk as EventDto['risk']}/></Link>)}</div>:
        <EmptyState title="No high-risk incidents" text="No CRITICAL or HIGH incidents were returned."/ >}</Card>
      <Card><div className="flex items-center justify-between gap-3"><h2 className="font-semibold">Active and recent alerts</h2><Link className="text-sm text-blue-700 underline" to="/alerts">View notifications</Link></div>
        {recentAlertQuery.isLoading?<Skeleton className="mt-3"/>:recentAlertQuery.isError?<ErrorState title="Unable to load alerts" text={normalizeApiError(recentAlertQuery.error)} onRetry={()=>recentAlertQuery.refetch()}/>:dashboardAlerts.length?
          <div className="mt-3 space-y-2">{dashboardAlerts.map(({alert,subject})=><div key={alert.id} className={`rounded-lg border p-3 text-sm ${alert.severity==='CRITICAL'?'border-red-300 bg-red-50':''}`}><div className="flex flex-wrap items-center gap-2"><b>{alertPlayerLabel(alert,subject as EventDto|undefined,subject as CollisionDto|undefined)}</b><RiskBadge level={alert.severity}/>{!alert.is_read&&<Badge tone="amber">Unread</Badge>}</div><p className="mt-1 text-slate-600">{safeAlertText(alert.title??alert.alert_type)}</p></div>)}</div>:
          <EmptyState title="No alerts" text="No alert records were returned."/ >}
      </Card>
      <Card><h2 className="font-semibold">Player-related incidents</h2>{eventQuery.isLoading||collisionQuery.isLoading?<Skeleton className="mt-3"/>:eventQuery.isError||collisionQuery.isError?<ErrorState title="Unable to load player incidents" text={normalizeApiError(eventQuery.error??collisionQuery.error)}/>:playerIncidents.length?
        <div className="mt-3 divide-y">{playerIncidents.map(([name,count])=><div key={name} className="flex justify-between py-2 text-sm"><span>{name}</span><span>{count} incident{count===1?'':'s'}</span></div>)}</div>:
        <EmptyState title="No reliably identified players" text="Uncertain identities are not attributed to a named player."/ >}
      </Card>
      <Card><h2 className="font-semibold">Quick actions</h2><div className="mt-3 flex flex-col gap-2">{QA[role].map(([label,to])=><Link key={label} to={role==='PLAYER'&&to==='/players/me'&&user?.playerId?`/players/${user.playerId}`:to} className={btnS}>{label}</Link>)}</div>
        {role==='ADMIN'&&<Link className="mt-3 inline-block text-sm text-blue-700 underline" to="/admin/system">Open system status</Link>}
      </Card>
    </div></>
}

type ReportRow={
 id:number;kind:string;player:string;risk:string|null;region:string|null;matchId:number|null;matchName:string;
 videoName:string|null;timestamp:string|null;videoTime:number|null;frame:number|null;evidence:string|null;href:string;
}
function ReportMetric({label,value,loading,error}:{label:string;value:number|null;loading:boolean;error:boolean}){
 return <Card><p className="text-xs text-slate-500">{label}</p>{loading?<Skeleton className="mt-2 h-8 w-12"/>:error||value===null?
      <p className="mt-2 text-sm text-slate-500">Unavailable</p>:<p className="mt-1 text-3xl font-bold">{value}</p>}</Card>
}
function reportRows(eventsFound:EventDto[],collisionsFound:CollisionDto[],matchNames:Map<number,string>,videoNames:Map<number,string>):ReportRow[]{
 const collisionsById=new Map(collisionsFound.map(collision=>[collision.id,collision]))
 const injuryRows=eventsFound.map(event=>({
      id:event.id,kind:event.event_type,player:eventPlayer(event).name,risk:event.risk,region:event.region,
      matchId:event.match_session_id,matchName:eventMatchLabel(event.match_name,event.match_session_id,matchNames),
      videoName:event.video_id===null?null:videoNames.get(event.video_id)??'Uploaded video',
      timestamp:event.timestamp,videoTime:event.video_time_sec,frame:event.frame_number,
      evidence:event.clip_url??(event.collision_event_id===null?null:collisionsById.get(event.collision_event_id)?.clip_url??null),
      href:`/events/${event.id}`,
 }))
 const collisionReportRows=collisionsFound.map(collision=>({
      id:collision.id,kind:collision.event_type,player:collisionPlayerLabel(collision),risk:collision.risk,region:collision.region,
      matchId:collision.match_session_id,matchName:eventMatchLabel(null,collision.match_session_id,matchNames),
      videoName:collision.video_id===null?null:videoNames.get(collision.video_id)??'Uploaded video',
      timestamp:collision.timestamp,videoTime:collision.video_time_sec,frame:collision.frame_number,evidence:collision.clip_url,
      href:`/collisions/${collision.id}`,
 }))
 return sortRecent([...injuryRows,...collisionReportRows])
}
export function Reports(){
 const [matchFilter,setMatchFilter]=useState(''),[fromFilter,setFromFilter]=useState(''),[toFilter,setToFilter]=useState('')
 const matchQuery=useQuery({queryKey:['reports','matches'],queryFn:()=>matches.list()})
 const eventQuery=useQuery({queryKey:['reports','events'],queryFn:()=>events.listAll()})
 const collisionQuery=useQuery({queryKey:['reports','collisions'],queryFn:()=>collisions.listAll()})
 const videoQuery=useQuery({queryKey:['reports','videos'],queryFn:()=>videos.listAll()})
 const matchesFound=matchQuery.data??[]
 const matchNames=new Map(matchesFound.map(match=>[match.id,matchLabel(match)]))
 const videoNames=new Map((videoQuery.data??[]).map(video=>[video.id,video.original_filename??'Uploaded video']))
 const records=reportRows(eventQuery.data??[],collisionQuery.data??[],matchNames,videoNames).filter(row=>{
      if(matchFilter&&row.matchId!==Number(matchFilter))return false
      const date=row.timestamp?.slice(0,10)??''
      if(fromFilter&&(!date||date<fromFilter))return false
      if(toFilter&&(!date||date>toFilter))return false
      return true
 })
 const eventError=eventQuery.isError||collisionQuery.isError
 const recordsLoading=eventQuery.isLoading||collisionQuery.isLoading
 const recordFailure=eventError
 const invalidDateRange=!!fromFilter&&!!toFilter&&fromFilter>toFilter
 const collisionIds=new Set(collisionQuery.data?.map(collision=>collision.id)??[])
 const selectedMatch=matchesFound.find(match=>String(match.id)===matchFilter)
 const filteredEvents=(eventQuery.data??[]).filter(event=>
      (!matchFilter||event.match_session_id===Number(matchFilter))&&isWithinDateRange(event.timestamp,fromFilter,toFilter))
 const filteredCollisions=(collisionQuery.data??[]).filter(collision=>
      (!matchFilter||collision.match_session_id===Number(matchFilter))&&isWithinDateRange(collision.timestamp,fromFilter,toFilter))
 const reportSummary=summarizeIncidents(filteredEvents,filteredCollisions)
 const playerIncidentCounts=new Map<string,number>()
 const countedPlayers=new Set<string>()
 const addPlayerIncident=(incidentKey:string,playerId:number|null,name:string)=>{
      if(playerId===null||name==='Player')return
      const key=`${incidentKey}:${playerId}`
      if(countedPlayers.has(key))return
      countedPlayers.add(key)
      playerIncidentCounts.set(name,(playerIncidentCounts.get(name)??0)+1)
 }
 for(const event of filteredEvents){
      const player=eventPlayer(event)
      addPlayerIncident(event.collision_event_id!==null&&collisionIds.has(event.collision_event_id)?`collision:${event.collision_event_id}`:`event:${event.id}`,player.playerId,player.name)
 }
 for(const collision of filteredCollisions)for(const participant of collisionParticipants(collision)??[]){
      const player=collisionParticipantPlayer(participant)
      addPlayerIncident(`collision:${collision.id}`,player.playerId,player.name)
 }
 const uniquePlayers=new Set(playerIncidentCounts.keys())
 const regions=new Map<string,number>()
 for(const row of records)regions.set(row.region?.trim()||'Unspecified',(regions.get(row.region?.trim()||'Unspecified')??0)+1)
 const timestamps=records.map(row=>row.timestamp).filter((value):value is string=>!!value).sort()
 const periodStart=fromFilter||selectedMatch?.date?.slice(0,10)||timestamps[0]?.slice(0,10)
 const periodEnd=toFilter||timestamps[timestamps.length-1]?.slice(0,10)||selectedMatch?.date?.slice(0,10)
 const evidenceCount=new Set(records.map(row=>row.evidence).filter((clip):clip is string=>!!clip)).size
 const reportVideoCount=(videoQuery.data??[]).filter(video=>
  (!matchFilter||video.match_session_id===Number(matchFilter))&&isWithinDateRange(video.created_at,fromFilter,toFilter)).length
 const displayedRows=records.slice(0,100)
 const refresh=()=>{void Promise.all([eventQuery.refetch(),collisionQuery.refetch(),matchQuery.refetch(),videoQuery.refetch()])}
 return <><PageHeader title="Reports" crumbs={[{label:'Reports'}]}/>
      <Card className="mb-4"><h2 className="font-semibold">Report scope</h2><div className="mt-3 grid gap-3 sm:grid-cols-3">
       <label className="text-sm"><span className="mb-1 block font-medium">Match / video</span><select className={inp} value={matchFilter} onChange={event=>setMatchFilter(event.target.value)}><option value="">All matches</option>{matchesFound.map(match=><option key={match.id} value={match.id}>{match.display_name}</option>)}</select></label>
       <label className="text-sm"><span className="mb-1 block font-medium">From date</span><input aria-label="Report start date" className={inp} type="date" value={fromFilter} onChange={event=>setFromFilter(event.target.value)}/></label>
       <label className="text-sm"><span className="mb-1 block font-medium">To date</span><input aria-label="Report end date" className={inp} type="date" value={toFilter} onChange={event=>setToFilter(event.target.value)}/></label>
      </div>{matchQuery.isError&&<p role="alert" className="mt-2 text-sm text-amber-800">Match names could not be loaded; unlinked match IDs are shown where available.</p>}
      {invalidDateRange&&<p role="alert" className="mt-2 text-sm text-red-700">The start date must be on or before the end date.</p>}
      <p className="mt-3 text-sm text-slate-600">Monitoring period: {periodStart??'Not recorded'} – {periodEnd??'Not recorded'}{selectedMatch?` · ${selectedMatch.display_name}`:''}</p></Card>
      {(eventQuery.isError||collisionQuery.isError||matchQuery.isError||videoQuery.isError)&&<ErrorState title="Some report data could not be loaded" text={normalizeApiError(eventQuery.error??collisionQuery.error??matchQuery.error??videoQuery.error)} onRetry={refresh}/>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
       <ReportMetric label="Total incidents" value={eventQuery.data&&collisionQuery.data?reportSummary.total:null} loading={recordsLoading} error={recordFailure}/>
       <ReportMetric label="CRITICAL" value={eventQuery.data&&collisionQuery.data?reportSummary.byRisk.CRITICAL:null} loading={recordsLoading} error={recordFailure}/>
       <ReportMetric label="HIGH" value={eventQuery.data&&collisionQuery.data?reportSummary.byRisk.HIGH:null} loading={recordsLoading} error={recordFailure}/>
       <ReportMetric label="MEDIUM" value={eventQuery.data&&collisionQuery.data?reportSummary.byRisk.MEDIUM:null} loading={recordsLoading} error={recordFailure}/>
       <ReportMetric label="LOW" value={eventQuery.data&&collisionQuery.data?reportSummary.byRisk.LOW:null} loading={recordsLoading} error={recordFailure}/>
       <ReportMetric label="Collisions" value={collisionQuery.data?filteredCollisions.length:null} loading={collisionQuery.isLoading} error={collisionQuery.isError}/>
       <ReportMetric label="High-risk incidents" value={eventQuery.data&&collisionQuery.data?reportSummary.byRisk.CRITICAL+reportSummary.byRisk.HIGH:null} loading={recordsLoading} error={recordFailure}/>
       <ReportMetric label="Reliable player identities" value={eventQuery.data&&collisionQuery.data?uniquePlayers.size:null} loading={recordsLoading} error={recordFailure}/>
       <ReportMetric label="Evidence clips" value={eventQuery.data&&collisionQuery.data?evidenceCount:null} loading={recordsLoading} error={recordFailure}/>
       <ReportMetric label="Videos" value={videoQuery.data?reportVideoCount:null} loading={videoQuery.isLoading} error={videoQuery.isError}/>
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
       <Card><h2 className="font-semibold">Body-region breakdown</h2>{recordsLoading?<Skeleton className="mt-3"/>:recordFailure?<p className="mt-2 text-sm text-slate-500">Region data is unavailable.</p>:regions.size?
        <div className="mt-3 divide-y">{[...regions.entries()].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).map(([region,count])=><div key={region} className="flex justify-between py-2 text-sm"><span>{region}</span><span>{count}</span></div>)}</div>:
        <EmptyState title="No incidents in this report"/>}</Card>
       <Card><h2 className="font-semibold">Reliable player incidents</h2>{recordsLoading?<Skeleton className="mt-3"/>:recordFailure?<p className="mt-2 text-sm text-slate-500">Player-linked incident data is unavailable.</p>:uniquePlayers.size?
        <div className="mt-3 space-y-2 text-sm">{[...playerIncidentCounts.entries()].sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).map(([name,count])=><p key={name}>{name} · {count} incident{count===1?'':'s'}</p>)}</div>:
        <EmptyState title="No reliably identified players" text="Uncertain identities remain unattributed."/ >}</Card>
      </div>
      <Card className="mt-4 !p-0"><div className="border-b p-4"><h2 className="font-semibold">Incident register</h2><p className="mt-1 text-xs text-slate-500">Medical assessments are generated on demand from stored incident metadata; they are not saved as separate records.</p></div>
       {recordsLoading?<div className="p-4"><Skeleton/></div>:recordFailure?<ErrorState title="Incident details unavailable" text={normalizeApiError(eventQuery.error??collisionQuery.error)} onRetry={refresh}/>:records.length?
       <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="bg-slate-50 text-xs uppercase text-slate-500"><tr>{['Match / video','Player','Incident','Risk','Region','Time / frame','Evidence','Assessment'].map(label=><th key={label} className="px-4 py-3">{label}</th>)}</tr></thead><tbody>{displayedRows.map(row=><tr key={`${row.href}-${row.id}`} className="border-t">
        <td className="px-4 py-3">{row.matchName}{row.videoName&&<span className="block text-xs text-slate-500">{row.videoName}</span>}</td><td className="px-4 py-3">{row.player}</td><td className="px-4 py-3"><Link className="text-blue-700 underline" to={row.href}>{row.kind} #{row.id}</Link></td>
        <td className="px-4 py-3"><RiskBadge level={row.risk as EventDto['risk']}/></td><td className="px-4 py-3">{row.region??'—'}</td>
        <td className="px-4 py-3">{row.timestamp?new Date(row.timestamp).toLocaleString():'Time not recorded'}{row.videoTime!==null?` · ${row.videoTime.toFixed(2)} sec`:''}{row.frame!==null?` · frame ${row.frame}`:''}</td>
        <td className="px-4 py-3">{row.evidence?<a className="text-blue-700 underline" href={row.evidence} target="_blank" rel="noreferrer">Available</a>:'Not attached'}</td>
        <td className="px-4 py-3"><Link className="text-blue-700 underline" to={row.href}>Available on demand</Link></td>
       </tr>)}</tbody></table>{records.length>displayedRows.length&&<p className="p-3 text-xs text-slate-500">Showing the most recent 100 of {records.length} incidents.</p>}</div>:
       <EmptyState title="No incidents in this report" text="Try a different match or monitoring period."/ >}
      </Card>
      <p className="mt-4"><Disclaimer/></p>
 </>
}

/* ---------- GENERIC LIST ---------- */
const CFG:Record<string,{title:string;cols:string[];base?:string;crumb:string;create?:string}>={teams:{title:'Teams',crumb:'Teams',base:'/teams',cols:['Team','Players','Recent incidents','Status'],create:'team'},players:{title:'Players',crumb:'Players',base:'/players',cols:['Name','Jersey','Position','Team','Status'],create:'player'},matches:{title:'Matches',crumb:'Matches',base:'/matches',cols:['Match','Date / time','Status'],create:'match'},events:{title:'Events',crumb:'Events',base:'/events',cols:['Event','Player','Match','Time','Risk','Status']},collisions:{title:'Collisions',crumb:'Collisions',base:'/collisions',cols:['Collision','Player A','Player B','Time','Risk']},users:{title:'User management',crumb:'Users',cols:['Name','Role','Team','Status','Actions'],create:'user'}}
export function ListPage({k}:{k:string}){
 const c=CFG[k],{user}=useAuth(),[sp]=useSearchParams(),qc=useQueryClient(),[q,setQ]=useState(sp.get('q')??''),[page,setPage]=useState(1),[open,setOpen]=useState(false),[userCreateRole,setUserCreateRole]=useState('player'),[editingUser,setEditingUser]=useState<MeDto|null>(null)
 const [riskFilter,setRiskFilter]=useState(sp.get('risk')??''),[eventTypeFilter,setEventTypeFilter]=useState(sp.get('event_type')??'')
 const [matchFilter,setMatchFilter]=useState(sp.get('match_id')??''),[playerFilter,setPlayerFilter]=useState(sp.get('player_id')??'')
 const [regionFilter,setRegionFilter]=useState(''),[fromFilter,setFromFilter]=useState(''),[toFilter,setToFilter]=useState('')
 const needsIncidentFilters=k==='events'||k==='collisions'
 const matchOptions=useQuery({queryKey:['list-filter-matches'],queryFn:()=>matches.list(),enabled:needsIncidentFilters&&isConnected})
 const playerOptions=useQuery({queryKey:['list-filter-players'],queryFn:()=>players.list(),enabled:needsIncidentFilters&&isConnected})
 const accountTeams=useQuery({queryKey:['user-create-teams'],queryFn:()=>teams.list(),enabled:k==='users'&&open})
 const accountPlayers=useQuery({queryKey:['user-create-player-profiles'],queryFn:()=>players.list(),enabled:k==='users'&&open})
 const query=useQuery({queryKey:['list',k,sp.toString(),matchFilter,playerFilter],queryFn:async()=>{
   switch(k){
    case'teams':return teams.list()
    case'players':return players.list()
    case'matches':return matches.list()
    case'events':return events.listAll({match_id:matchFilter?Number(matchFilter):undefined,player_id:playerFilter?Number(playerFilter):undefined,resolved:sp.has('resolved')?sp.get('resolved')==='true':undefined})
    case'collisions':return collisions.listAll({match_id:matchFilter?Number(matchFilter):undefined,player_id:playerFilter?Number(playerFilter):undefined})
    case'users':return users.list()
    default:return []
   }
 }})
 const mutation=useMutation({mutationFn:async(v:Record<string,string>)=>{switch(k){case'teams':return teams.create({name:v.name,short_name:v.short_name||null});case'players':return players.create({name:v.name,team_id:Number(v.team_id),jersey_number:Number(v.jersey_number),position:v.position||null});case'matches':return matches.create({name:v.name||null,team_a_id:v.team_a_id?Number(v.team_a_id):null,team_b_id:v.team_b_id?Number(v.team_b_id):null,date:v.date?new Date(v.date).toISOString():null});case'users':return users.register({username:v.username,password:v.password,role:v.role.toLowerCase(),team_id:v.team_id?Number(v.team_id):null,player_id:v.role==='player'&&v.player_id?Number(v.player_id):null,full_name:v.full_name||null,email:v.email||null,phone:v.phone||null});default:throw new Error('Unsupported operation')}},onSuccess:()=>{setOpen(false);qc.invalidateQueries({queryKey:['list']})}})
 const updateUserMutation=useMutation({mutationFn:({id,body}:{id:number;body:{email?:string|null;full_name?:string|null;phone?:string|null;active?:boolean;password?:string}})=>users.update(id,body),onSuccess:()=>{setEditingUser(null);qc.invalidateQueries({queryKey:['list','users']})}})
 const rows=(query.data??[]).filter(x=>{
  if(!JSON.stringify(x).toLowerCase().includes(q.toLowerCase()))return false
  if(!needsIncidentFilters)return true
  const record=x as EventDto|CollisionDto
  if(riskFilter&&record.risk?.toUpperCase()!==riskFilter.toUpperCase())return false
  if(eventTypeFilter&&record.event_type.toUpperCase()!==eventTypeFilter.toUpperCase())return false
  if(regionFilter&&(record.region??'').toLowerCase()!==regionFilter.toLowerCase())return false
  const date=record.timestamp?.slice(0,10)??''
  if(fromFilter&&(!date||date<fromFilter))return false
  if(toFilter&&(!date||date>toFilter))return false
  return true
 }),create=!!c.create&&user?.role==='ADMIN'
 const eventTypes=[...new Set((query.data??[]).map(row=>(row as EventDto|CollisionDto).event_type).filter(Boolean))].sort()
 const regions=[...new Set((query.data??[]).map(row=>(row as EventDto|CollisionDto).region).filter((region):region is string=>!!region))].sort()
 const fields:Record<string,[string,string,string?][]>= {team:[['name','Team name'],['short_name','Short name']],player:[['name','Player name'],['team_id','Team ID','number'],['jersey_number','Jersey number','number'],['position','Position']],match:[['name','Match name'],['team_a_id','Team A ID','number'],['team_b_id','Team B ID','number'],['date','Date and time','datetime-local']],user:[['username','Username'],['password','Password','password'],['role','Role'],['team_id','Team'],['player_id','Player profile'],['full_name','Full name'],['email','Email'],['phone','Phone']]}
 const values:Record<string,string>={Team:'name',Players:'player_count','Recent incidents':'injury_events',Name:'name',Jersey:'jersey_number',Position:'position',Match:'display_name','Date / time':'date',Event:'event_type',Player:'player_name',Time:'video_time_sec',Risk:'risk',Status:'status',Role:'role'}
 return <><PageHeader title={c.title} crumbs={[{label:c.crumb}]} actions={create?<button className={btnP} onClick={()=>{setUserCreateRole('player');setOpen(true)}}><Plus size={16}/>Create</button>:undefined}/><Card className="!p-0"><div className="flex flex-wrap gap-2 border-b p-3"><input aria-label={'Search '+c.title} className={inp+' max-w-xs'} placeholder="Search..." value={q} onChange={e=>{setQ(e.target.value);setPage(1)}}/>
  {needsIncidentFilters&&<><select aria-label="Severity filter" className={inp+' w-auto'} value={riskFilter} onChange={e=>{setRiskFilter(e.target.value);setPage(1)}}><option value="">All risk levels</option>{['CRITICAL','HIGH','MEDIUM','LOW'].map(level=><option key={level}>{level}</option>)}</select>
   <select aria-label="Event type filter" className={inp+' w-auto'} value={eventTypeFilter} onChange={e=>{setEventTypeFilter(e.target.value);setPage(1)}}><option value="">All event types</option>{eventTypes.map(type=><option key={type}>{type}</option>)}</select>
   <select aria-label="Body region filter" className={inp+' w-auto'} value={regionFilter} onChange={e=>{setRegionFilter(e.target.value);setPage(1)}}><option value="">All regions</option>{regions.map(region=><option key={region}>{region}</option>)}</select>
   <select aria-label="Match filter" className={inp+' w-auto'} value={matchFilter} onChange={e=>{setMatchFilter(e.target.value);setPage(1)}}><option value="">All matches</option>{(matchOptions.data??[]).map(match=><option key={match.id} value={match.id}>{matchLabel(match)}</option>)}</select>
   <select aria-label="Player filter" className={inp+' w-auto'} value={playerFilter} onChange={e=>{setPlayerFilter(e.target.value);setPage(1)}}><option value="">All players</option>{(playerOptions.data??[]).map(player=><option key={player.id} value={player.id}>{player.name}</option>)}</select>
   <label className="flex items-center gap-1 text-xs text-slate-600">From <input aria-label="Start date" className={inp+' w-auto'} type="date" value={fromFilter} onChange={e=>{setFromFilter(e.target.value);setPage(1)}}/></label>
   <label className="flex items-center gap-1 text-xs text-slate-600">To <input aria-label="End date" className={inp+' w-auto'} type="date" value={toFilter} onChange={e=>{setToFilter(e.target.value);setPage(1)}}/></label></>}
  </div>{needsIncidentFilters&&(matchOptions.isError||playerOptions.isError)&&<p role="status" className="px-3 pb-2 text-xs text-amber-800">Some match or player filter options could not be loaded; incident data remains available.</p>}<div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="bg-slate-50 text-xs uppercase text-slate-500"><tr>{c.cols.map(h=><th key={h} className="px-4 py-3">{h}</th>)}</tr></thead><tbody>{query.isLoading&&[0,1,2].map(i=><tr key={i}><td colSpan={c.cols.length} className="px-4 py-3"><Skeleton/></td></tr>)}{!query.isLoading&&!query.isError&&rows.slice((page-1)*10,page*10).map((r,i)=>{const o=r as unknown as Record<string,unknown>,collisionParticipantsForRow=k==='collisions'?collisionParticipants(o as unknown as CollisionDto):null;return <tr key={String(o.id??i)} className="border-t">{c.cols.map((h,j)=><td key={h} className="px-4 py-3">{k==='users'&&h==='Actions'?<button className="text-blue-700 underline" onClick={()=>setEditingUser(o as unknown as MeDto)}>Manage</button>:k==='users'&&h==='Team'?o.team_id===null?'—':`Team #${String(o.team_id)}`:k==='users'&&h==='Status'?o.active?'Active':'Inactive':j===0&&c.base&&o.id?<Link className="text-blue-700 underline" to={c.base+'/'+o.id}>{String(o[values[h]??'name']??o.display_name??o.username??o.id)}</Link>:k==='events'&&h==='Player'?eventPlayer(o as unknown as EventDto).name:k==='collisions'&&h==='Player A'?collisionParticipantsForRow?collisionParticipantPlayer(collisionParticipantsForRow[0]).name:'Player':k==='collisions'&&h==='Player B'?collisionParticipantsForRow?collisionParticipantPlayer(collisionParticipantsForRow[1]).name:'Player':<Na v={o[values[h]??(h==='Name'?'username':h.toLowerCase())] as string|number|null|undefined}/>}</td>)}</tr>})}</tbody></table></div>{query.isError&&<ErrorState title={'Unable to load '+c.title.toLowerCase()} text={normalizeApiError(query.error)} onRetry={()=>query.refetch()}/ >}{!query.isLoading&&!query.isError&&!rows.length&&<EmptyState title={'No '+c.title.toLowerCase()+' found'} text="No records found."/>}<div className="flex items-center justify-between border-t p-3 text-sm"><span>{rows.length} result(s)</span><div><button className={btnS} disabled={page===1} onClick={()=>setPage(page-1)}>Previous</button> Page {page} <button className={btnS} disabled={page*10>=rows.length} onClick={()=>setPage(page+1)}>Next</button></div></div></Card>{mutation.isError&&<p role="alert" className="mt-2 text-sm text-red-700">{normalizeApiError(mutation.error)}</p>}{create&&<Modal open={open} title={'Create '+c.create} onClose={()=>setOpen(false)}><form className="space-y-3" onSubmit={e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.currentTarget).entries());mutation.mutate(Object.fromEntries(Object.entries(data).map(([x,y])=>[x,String(y)])))}}>{fields[c.create!].filter(([name])=>c.create!=='user'||(name!=='team_id'||userCreateRole==='coach')&&(name!=='player_id'||userCreateRole==='player')).map(([name,label,type])=><Field key={name} label={label}>{c.create==='user'&&name==='role'?<select className={inp} name={name} value={userCreateRole} onChange={e=>setUserCreateRole(e.target.value)}><option value="player">Player</option><option value="coach">Coach</option><option value="medical">Medical</option><option value="admin">Admin</option></select>:c.create==='user'&&name==='team_id'?<select className={inp} name={name} required defaultValue=""><option value="">Select team</option>{(accountTeams.data??[]).map(team=><option key={team.id} value={team.id}>{team.name}</option>)}</select>:c.create==='user'&&name==='player_id'?<select className={inp} name={name} required><option value="">Select player profile</option>{(accountPlayers.data??[]).map(player=><option key={player.id} value={player.id}>{player.name} · #{player.jersey_number}</option>)}</select>:<input className={inp} name={name} type={type??'text'} required={['name','username','password','team_id','jersey_number','role'].includes(name)}/>}</Field>)}<p className="text-xs text-slate-600">Player accounts must link to an existing roster profile. Coach accounts must be assigned to a team.</p>{mutation.isError&&<p role="alert" className="text-sm text-red-700">{normalizeApiError(mutation.error)}</p>}<div className="flex justify-end gap-2"><button type="button" className={btnS} onClick={()=>setOpen(false)}>Cancel</button><button className={btnP} disabled={mutation.isPending}>{mutation.isPending?'Saving…':'Save'}</button></div></form></Modal>}{editingUser&&<Modal open title={`Manage ${editingUser.username}`} onClose={()=>setEditingUser(null)}><form className="space-y-3" onSubmit={e=>{e.preventDefault();const form=new FormData(e.currentTarget);const password=String(form.get('password')??'');updateUserMutation.mutate({id:editingUser.id,body:{email:String(form.get('email')??'')||null,full_name:String(form.get('full_name')??'')||null,phone:String(form.get('phone')??'')||null,active:form.get('active')==='on',...(password?{password}:{})}})}}><Field label="Username"><input className={inp} value={editingUser.username} readOnly/></Field><Field label="Full name"><input className={inp} name="full_name" defaultValue={editingUser.full_name??''}/></Field><Field label="Email"><input className={inp} type="email" name="email" defaultValue={editingUser.email??''}/></Field><Field label="Phone"><input className={inp} name="phone" defaultValue={editingUser.phone??''}/></Field><Field label="Set a temporary password"><input className={inp} type="password" name="password" autoComplete="new-password" minLength={8}/></Field><label className="flex items-center gap-2 text-sm"><input type="checkbox" name="active" defaultChecked={editingUser.active}/> Account active</label><p className="text-xs text-slate-600">Setting a temporary password is administrator-assisted recovery; no email reset is configured.</p>{updateUserMutation.isError&&<p role="alert" className="text-sm text-red-700">{normalizeApiError(updateUserMutation.error)}</p>}<div className="flex justify-end gap-2"><button type="button" className={btnS} onClick={()=>setEditingUser(null)}>Cancel</button><button className={btnP} disabled={updateUserMutation.isPending}>{updateUserMutation.isPending?'Saving…':'Save account'}</button></div></form></Modal>}</>
}/* ---------- GENERIC DETAIL ---------- */
export function Detail({k}:{k:'teams'|'players'}){
 const {id}=useParams(),{user}=useAuth(),routeId=id==='me'&&user?.role==='PLAYER'?user.playerId?.toString():id,n=Number(routeId),qc=useQueryClient(),[tab,setTab]=useState('Overview')
 const query=useQuery<TeamDto|PlayerDto>({queryKey:[k,'detail',n],enabled:Number.isFinite(n),queryFn:()=>k==='teams'?teams.get(n):players.get(n)})
 const roster=useQuery({queryKey:['teams',n,'players'],enabled:k==='teams'&&tab==='Roster',queryFn:()=>teams.players(n)})
 const stats=useQuery({queryKey:['players',n,'stats'],enabled:k==='players'&&tab==='Statistics',queryFn:()=>players.stats(n)})
 const incidents=useQuery({queryKey:['players',n,'events'],enabled:k==='players'&&tab==='Incidents',queryFn:()=>events.list({player_id:n,limit:100,offset:0})})
 const playerMatches=useQuery({queryKey:['players',n,'matches'],enabled:k==='players'&&tab==='Matches',queryFn:()=>matches.list()})
 const deactivate=useMutation({mutationFn:()=>players.deactivate(n),onSuccess:()=>{qc.invalidateQueries({queryKey:['list','players']});qc.invalidateQueries({queryKey:['players','detail',n]})}})
 const tabs=k==='players'?['Overview','Incidents','Matches','Statistics','Reference photos']:['Overview','Roster','Statistics']
 const data=Object.fromEntries(Object.entries(query.data??{}).filter(([key])=>!['id','team_id','face_embedding','reference_images'].includes(key))) as Record<string,unknown>
 const title=String(data.name??data.display_name??(k==='teams'?'Team':'Player'))
 const detailCrumb=k==='players'?(user?.role==='PLAYER'&&user.playerId===n?'My profile':'Details'):`#${id}`
 return <>
  <PageHeader title={title} crumbs={[{label:k,to:'/'+k},{label:detailCrumb}]} actions={k==='players'&&user?.role==='ADMIN'?<button className={btnS} onClick={()=>deactivate.mutate()}>Deactivate</button>:undefined}/>
  <div role="tablist" className="mb-4 flex gap-1 overflow-x-auto border-b">{tabs.map(t=><button key={t} role="tab" aria-selected={tab===t} onClick={()=>setTab(t)} className={'px-4 py-2 text-sm '+(tab===t?'border-b-2 border-[#0b1f3a] font-semibold':'text-slate-500')}>{t}</button>)}</div>
  <Card>{query.isLoading?<Skeleton/>:query.isError?<ErrorState title="Unable to load record" text={normalizeApiError(query.error)} onRetry={()=>query.refetch()}/>:k==='players'&&user?.role==='PLAYER'&&!user.playerId?
   <EmptyState title="Player profile unavailable" text="This account is not linked to an active player profile."/>:
   tab==='Roster'&&k==='teams'?
   roster.isLoading?<Skeleton/>:roster.isError?<ErrorState title="Unable to load team roster" text={normalizeApiError(roster.error)} onRetry={()=>roster.refetch()}/>:<div className="space-y-2">{(roster.data??[]).map(p=><p key={p.id}>{p.name} · #{p.jersey_number}</p>)}{!roster.data?.length&&<EmptyState title="No players in this team"/>}</div>:
   tab==='Incidents'&&k==='players'?
   incidents.isLoading?<Skeleton/>:incidents.isError?<ErrorState title="Unable to load player incidents" text={normalizeApiError(incidents.error)} onRetry={()=>incidents.refetch()}/>:incidents.data?.length?<div className="divide-y">{incidents.data.map(event=><Link key={event.id} to={`/events/${event.id}`} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm hover:bg-slate-50"><span>{event.event_type} · {eventPlayer(event).name}</span><span className="text-slate-500">{event.risk??'Unrated'}</span></Link>)}</div>:<EmptyState title="No incidents recorded for this player"/>:
   tab==='Matches'&&k==='players'?
   playerMatches.isLoading?<Skeleton/>:playerMatches.isError?<ErrorState title="Unable to load player matches" text={normalizeApiError(playerMatches.error)} onRetry={()=>playerMatches.refetch()}/>:playerMatches.data?.length?<div className="divide-y">{playerMatches.data.map(match=><Link key={match.id} to={`/matches/${match.id}`} className="flex items-center justify-between gap-2 py-3 text-sm hover:bg-slate-50"><span>{match.display_name}</span><span className="text-slate-500">{matchStatusLabel(match.status)}</span></Link>)}</div>:<EmptyState title="No matches available for this team"/>:
   tab==='Overview'?<dl className="grid gap-3 sm:grid-cols-2">{Object.entries(data).map(([key,value])=><div key={key}><dt className="text-xs uppercase text-slate-500">{key.replace(/_/g,' ')}</dt><dd className="text-sm">{value===null?'—':String(value)}</dd></div>)}</dl>:
   tab==='Statistics'&&k==='players'?stats.isLoading?<Skeleton/>:stats.isError?<ErrorState title="Unable to load player statistics" text={normalizeApiError(stats.error)} onRetry={()=>stats.refetch()}/>:stats.data?<dl className="grid gap-3 sm:grid-cols-2"><div><dt className="text-xs uppercase text-slate-500">Player</dt><dd>{stats.data.name}</dd></div><div><dt className="text-xs uppercase text-slate-500">Jersey</dt><dd>{stats.data.jersey_number}</dd></div><div><dt className="text-xs uppercase text-slate-500">Incidents</dt><dd>{stats.data.injury_events}</dd></div><div><dt className="text-xs uppercase text-slate-500">High-risk incidents</dt><dd>{stats.data.high_risk_events}</dd></div></dl>:<EmptyState title="No player statistics available"/>:
   <EmptyState title={tab+' unavailable'} text="This backend does not provide this data."/>}</Card>
  {deactivate.isError&&<p role="alert" className="mt-2 text-sm text-red-700">{normalizeApiError(deactivate.error)}</p>}
 </>
}/* ---------- MATCH ---------- */
function matchStatusLabel(status?:string|null){const labels:Record<string,string>={CREATED:'Ready',VIDEO_UPLOADED:'Uploaded',PROCESSING:'Processing',COMPLETED:'Completed',FAILED:'Failed'};return status?labels[status]??status:'Unknown'}
function videoStatusLabel(status?:string|null){const labels:Record<string,string>={UPLOADED:'Uploaded',PROCESSING:'Processing',PROCESSED:'Completed',FAILED:'Failed'};return status?labels[status]??status:'Unknown'}
export function MatchDetail(){
 const {id}=useParams(),n=Number(id),{user}=useAuth(),qc=useQueryClient(),[file,setFile]=useState<File|null>(null),[progress,setProgress]=useState(0)
 const match=useQuery({queryKey:['matches','detail',n],queryFn:()=>matches.get(n),enabled:Number.isFinite(n),refetchInterval:q=>q.state.data?.status==='PROCESSING'?2000:false})
 const matchTeams=useQuery({queryKey:['match-detail','teams'],queryFn:()=>teams.list(),enabled:user?.role==='ADMIN'||user?.role==='COACH'})
 const video=useQuery({queryKey:['videos',n],queryFn:()=>videos.list({match_id:n,limit:100,offset:0}),enabled:Number.isFinite(n)&&user?.role!=='PLAYER',refetchInterval:()=>match.data?.status==='PROCESSING'?2000:false})
 const upload=useMutation({mutationFn:(selected:File)=>videos.upload(n,selected,setProgress),onSuccess:async()=>{setFile(null);await Promise.all([qc.invalidateQueries({queryKey:['videos',n]}),qc.invalidateQueries({queryKey:['matches','detail',n]}),qc.invalidateQueries({queryKey:['list','matches']})])}})
 const process=useMutation({mutationFn:()=>matches.start(n),onSuccess:async()=>{await Promise.all([qc.invalidateQueries({queryKey:['videos',n]}),qc.invalidateQueries({queryKey:['matches','detail',n]}),qc.invalidateQueries({queryKey:['list','matches']})])}})
 const m=match.data,admin=user?.role==='ADMIN',records=video.data??[]
 const teamLabel=(teamId:number|null|undefined)=>teamId==null?'—':user?.role==='PLAYER'?teamId===user.teamId?'Your team':'Opponent':matchTeams.data?.find(team=>team.id===teamId)?.name??'Team'
 return <>
  <PageHeader title={matchLabel(m)} crumbs={[{label:'Matches',to:'/matches'},{label:matchLabel(m)}]} actions={user?.role!=='PLAYER'?<Link className={btnP} to={`/matches/${id}/monitor`}>Open monitor</Link>:undefined}/>
  {match.isLoading?<Card><Skeleton/></Card>:match.isError?<ErrorState title="Unable to load match" text={normalizeApiError(match.error)} onRetry={()=>match.refetch()}/>:<>
   <Card className="mb-4 text-center"><div className="flex items-center justify-center gap-6 text-xl font-bold"><span>{teamLabel(m?.team_a_id)}</span><span className="text-slate-400">vs</span><span>{teamLabel(m?.team_b_id)}</span></div><p className="mt-1 text-sm text-slate-500">{m?.date??'Date not set'} · <Badge>{matchStatusLabel(m?.status)}</Badge></p></Card>
   <div className="grid gap-4 lg:grid-cols-2">
    <Card><h2 className="font-semibold">Video input</h2>{admin?<><p className="mt-2 text-sm text-slate-600">Choose a supported video file. The server validates, stores it, and creates a video record for this match.</p><input aria-label="Choose match video" className="mt-3 block w-full text-sm" type="file" accept=".mp4,.avi,.mov,.mkv,.webm,video/*" onChange={e=>{setFile(e.target.files?.[0]??null);setProgress(0);upload.reset()}}/><p className="mt-1 text-xs text-slate-500">Supported extensions: MP4, AVI, MOV, MKV, WEBM. Size limits are enforced by the server.</p><button className={btnP+' mt-3'} disabled={!file||upload.isPending} onClick={()=>file&&upload.mutate(file)}>{upload.isPending?`Uploading ${progress}%`:'Upload video'}</button>{upload.isError&&<p role="alert" className="mt-2 text-sm text-red-700">{normalizeApiError(upload.error)}</p>}{upload.isSuccess&&<p role="status" className="mt-2 text-sm text-green-800">Server accepted the video for this match. Upload status: {videoStatusLabel(upload.data.status)}.</p>}</>:<p className="mt-2 text-sm text-slate-500">Only administrators can upload video.</p>}</Card>
    {user?.role==='PLAYER'?<Card><h2 className="font-semibold">Video records</h2><p className="mt-2 text-sm text-slate-500">Video metadata and playback are restricted. Your permitted events and results are available from Incidents.</p></Card>:<Card><h2 className="font-semibold">Video records</h2>{video.isLoading?<Skeleton/>:video.isError?<ErrorState title="Unable to load match videos" text={normalizeApiError(video.error)} onRetry={()=>video.refetch()}/>:records.length?<div className="space-y-3">{records.map(v=><div key={v.id} className="rounded-lg border p-3"><div className="flex flex-wrap items-center justify-between gap-2"><b>{v.original_filename??'Uploaded video'}</b><Badge>{videoStatusLabel(v.status)}</Badge></div><p className="mt-1 text-xs text-slate-600">{v.file_size_bytes??'—'} bytes · {v.duration_sec===null?'Duration unknown':`${v.duration_sec.toFixed(1)} sec`}</p>{v.error_message&&<p className="mt-1 text-sm text-red-700">{v.error_message}</p>}</div>)}</div>:<EmptyState title="No video uploaded" text="Select a video file to create a server-side video record for this match."/>}</Card>}
   </div>
   <Card className="mt-4"><h2 className="font-semibold">Processing status</h2><p className="mt-2 text-sm">Match: <Badge>{matchStatusLabel(m?.status)}</Badge>{records[0]&&<> · Video: <Badge>{videoStatusLabel(records[0].status)}</Badge></>}</p>{admin&&records[0]&&<button className={btnP+' mt-3'} disabled={process.isPending||m?.status==='PROCESSING'||m?.status==='COMPLETED'} onClick={()=>process.mutate()}>{process.isPending?'Starting…':m?.status==='PROCESSING'?'AI screening in progress':'Start AI screening'}</button>}{process.isError&&<p role="alert" className="mt-2 text-sm text-red-700">{normalizeApiError(process.error)}</p>}<p className="mt-2 text-sm text-slate-600">AI screening runs on the backend with YOLO11-Pose. Results are screening signals, not medical diagnoses.</p></Card>
  </>}
 </>
}
export function Monitor(){
 const {id}=useParams(),n=Number(id),[selected,setSelected]=useState<number|null>(null),{user}=useAuth()
 const match=useQuery({queryKey:['matches','detail',n],queryFn:()=>matches.get(n),enabled:Number.isFinite(n),refetchInterval:q=>q.state.data?.status==='PROCESSING'?2000:false})
 const refresh=()=>match.data?.status==='PROCESSING'?2000:false
 const video=useQuery({queryKey:['videos',n],queryFn:()=>videos.list({match_id:n,limit:100,offset:0}),enabled:Number.isFinite(n),refetchInterval:refresh})
 const eventQuery=useQuery({queryKey:['events','match',n],queryFn:()=>events.list({match_id:n,limit:100,offset:0}),enabled:Number.isFinite(n),refetchInterval:refresh})
 const collisionQuery=useQuery({queryKey:['collisions','match',n],queryFn:()=>collisions.list({match_id:n,limit:100,offset:0}),enabled:Number.isFinite(n),refetchInterval:refresh})
 const alertQuery=useQuery({queryKey:['alerts','match',n],queryFn:()=>alerts.list({match_id:n,limit:100,offset:0}),enabled:Number.isFinite(n),refetchInterval:refresh})
 const canViewTracks=['admin','medical'].includes((user?.role??'').toLowerCase())
 const trackQuery=useQuery({queryKey:['tracks','match',n],queryFn:()=>tracks.listForMatch(n),enabled:Number.isFinite(n)&&canViewTracks,refetchInterval:refresh})
 const rows=video.data??[],current=rows.find(v=>v.id===selected)??rows[0]
 return <>
  <PageHeader title={`Match monitor · ${matchLabel(match.data)}`} crumbs={[{label:'Matches',to:'/matches'},{label:matchLabel(match.data),to:`/matches/${id}`},{label:'Monitor'}]} actions={<Badge>{matchStatusLabel(match.data?.status)}</Badge>}/>
  {match.isLoading?<Card><Skeleton/></Card>:match.isError?<ErrorState title="Unable to load match" text={normalizeApiError(match.error)} onRetry={()=>match.refetch()}/>:<>
   <Card className="mb-4">
    <h2 className="font-semibold">{matchLabel(match.data)}</h2>
    <p className="mt-1 text-sm">Match status: {matchStatusLabel(match.data?.status)}</p>
    {video.isLoading?<Skeleton className="mt-3"/>:video.isError?<ErrorState title="Unable to load videos" text={normalizeApiError(video.error)} onRetry={()=>video.refetch()}/>:rows.length?<label className="mt-3 block text-sm">Selected video<select className={inp+' mt-1'} value={current?.id??''} onChange={e=>setSelected(Number(e.target.value))}>{rows.map(v=><option key={v.id} value={v.id}>{v.original_filename??'Uploaded video'} · {videoStatusLabel(v.status)}</option>)}</select></label>:<p className="mt-2 text-sm text-slate-500">No video record is attached to this match yet.</p>}
    {current&&<p className="mt-2 text-sm">Video status: <Badge>{videoStatusLabel(current.status)}</Badge>{current.error_message&&<span className="ml-2 text-red-700">{current.error_message}</span>}</p>}
   </Card>
   <div className="grid gap-4 lg:grid-cols-4">
    <Card><h2 className="font-semibold">Video preview</h2><div className="mt-3 grid aspect-video place-items-center rounded-xl bg-slate-900 p-4 text-center text-sm text-slate-300"><div><VideoOff className="mx-auto" size={34}/><p className="mt-2">No authorized full-video playback URL is provided by the backend.</p><p className="mt-1 text-xs text-slate-400">Signed event clips are available from each event detail when present.</p></div></div></Card>
    <Card>
     <h2 className="font-semibold">Detected players ({canViewTracks?trackQuery.data?.length??'?':'staff only'})</h2>
     {!canViewTracks?<p className="mt-2 text-sm text-slate-500">Player activity details are restricted to staff.</p>:trackQuery.isLoading?<Skeleton/>:trackQuery.isError?<ErrorState title="Unable to load players" text={normalizeApiError(trackQuery.error)} onRetry={()=>trackQuery.refetch()}/>:trackQuery.data?.length?<div className="mt-3 max-h-96 space-y-2 overflow-auto">{trackQuery.data.map((t:PlayerTrackDto)=><div key={t.id} className="rounded-lg border p-2 text-sm"><b>{playerLabel(t.player?.name,hasReliablePlayerIdentity(t.player_id,t.identified_by,t.id_confidence,t.identity_status==='CONFIRMED')&&t.player?.id===t.player_id)}</b><div className="text-xs text-slate-500">{t.first_seen_sec===null?'—':`${t.first_seen_sec.toFixed(1)} sec`} – {t.last_seen_sec===null?'—':`${t.last_seen_sec.toFixed(1)} sec`}</div></div>)}</div>:<EmptyState title="No player activity recorded yet."/>}
    </Card>
    <Card><h2 className="font-semibold">AI events</h2>{eventQuery.isLoading?<Skeleton/>:eventQuery.isError?<ErrorState title="Unable to load events" text={normalizeApiError(eventQuery.error)} onRetry={()=>eventQuery.refetch()}/>:eventQuery.data?.length?<div className="mt-3 space-y-2">{eventQuery.data.map(e=><Link key={e.id} to={`/events/${e.id}`} className="block rounded-lg border p-2 text-sm hover:bg-slate-50"><b>{e.event_type}</b> ? {eventPlayer(e).name}<div className="text-xs text-slate-500">{e.video_time_sec??'?'} sec ? {e.risk??'Unrated'}</div></Link>)}</div>:<EmptyState title="No AI events detected yet."/>}</Card>
    <Card><h2 className="font-semibold">Collisions and alerts</h2>{collisionQuery.isLoading||alertQuery.isLoading?<Skeleton/>:collisionQuery.isError?<ErrorState title="Unable to load collisions" text={normalizeApiError(collisionQuery.error)} onRetry={()=>collisionQuery.refetch()}/>:alertQuery.isError?<ErrorState title="Unable to load alerts" text={normalizeApiError(alertQuery.error)} onRetry={()=>alertQuery.refetch()}/>:<><h3 className="mt-3 text-sm font-medium">Collisions ({collisionQuery.data?.length??0})</h3>{collisionQuery.data?.map(c=><Link key={c.id} to={`/collisions/${c.id}`} className="mt-2 block text-sm underline">Possible player contact · {c.risk??'Unrated'}</Link>)}{!collisionQuery.data?.length&&<p className="mt-1 text-sm text-slate-500">No collisions detected yet.</p>}<h3 className="mt-4 text-sm font-medium">Alerts ({alertQuery.data?.length??0})</h3>{alertQuery.data?.map(a=><p key={a.id} className="mt-2 text-sm">{alertPlayerLabel(a,eventQuery.data?.find(e=>e.id===a.injury_event_id),collisionQuery.data?.find(c=>c.id===a.collision_event_id))} — {a.severity??'Unrated'}<span className="block text-xs text-slate-500">{safeAlertText(a.title??a.alert_type)}</span></p>)}{!alertQuery.data?.length&&<p className="mt-1 text-sm text-slate-500">No alerts for this match.</p>}</>}</Card>
   </div>
  </>}
 </>
}
/* ---------- EVENT / COLLISION DETAIL ---------- */
function MedicalScan({eventId,collisionId}:{eventId:number|null;collisionId:number|null}){
 const assessment=useQuery<MedicalAssessmentDto>({
  queryKey:['medical-assessment',eventId===null?'collision':'event',eventId??collisionId],
  enabled:eventId!==null||collisionId!==null,
  queryFn:()=>eventId!==null?medicalAssessments.forEvent(eventId):medicalAssessments.forCollision(collisionId!),
 })
 if(eventId===null&&collisionId===null)return null
 if(assessment.isLoading)return <Card><h2 className="font-semibold">AI injury-risk assessment</h2><Skeleton className="mt-3"/></Card>
 if(assessment.isError)return <Card><h2 className="font-semibold">AI injury-risk assessment</h2><ErrorState title="Unable to load assessment" text={normalizeApiError(assessment.error)} onRetry={()=>assessment.refetch()}/></Card>
 const scan=assessment.data
 if(!scan)return <Card><h2 className="font-semibold">AI injury-risk assessment</h2><p className="mt-2 text-sm text-slate-600">Assessment is unavailable.</p></Card>
 return <Card>
  <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">AI injury-risk assessment</h2><RiskBadge level={scan.risk_level}/></div>
  <p className="mt-3 text-lg font-semibold">{playerLabel(scan.player_name,scan.player_id!==null)}</p>
  <dl className="mt-3 space-y-3 text-sm">
   <div><dt className="font-medium text-slate-600">Suspected affected region</dt><dd>{scan.affected_region}</dd></div>
   <div><dt className="font-medium text-slate-600">AI assessment</dt><dd>{scan.assessment_summary}</dd></div>
   <div><dt className="font-medium text-slate-600">Observed signs</dt><dd><ul className="mt-1 list-disc space-y-1 pl-5">{scan.observed_signs.map((sign,i)=><li key={`${i}-${sign}`}>{sign}</li>)}</ul></dd></div>
   <div><dt className="font-medium text-slate-600">Recommended action</dt><dd>{scan.recommended_action}</dd></div>
   <div><dt className="font-medium text-slate-600">Medical attention</dt><dd>{scan.requires_medical_attention?'Professional medical attention recommended':'No urgent escalation indicated by this limited signal'}</dd></div>
   {scan.collision_context&&<div><dt className="font-medium text-slate-600">Collision context</dt><dd>{scan.collision_context}</dd></div>}
   <div><dt className="font-medium text-slate-600">Evidence</dt><dd>{scan.evidence_clip_url?<a className="text-blue-700 underline" href={scan.evidence_clip_url} target="_blank" rel="noreferrer">Review existing event clip</a>:scan.evidence_available?'A frame/time reference is recorded; no clip is attached.':'No clip or frame reference is attached; assessment uses stored event metadata.'}</dd></div>
  </dl>
  <p className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">{scan.disclaimer}</p>
 </Card>
}

export function EventDetail({collision=false}:{collision?:boolean}){
 const {id}=useParams(),n=Number(id),[clipError,setClipError]=useState(false)
 const q=useQuery<EventDto|CollisionDto>({queryKey:[collision?'collisions':'events','detail',n],enabled:Number.isFinite(n),queryFn:()=>collision?collisions.get(n):events.get(n)})
 const record=q.data
 const people=record&&collision?collisionParticipants(record as CollisionDto):null
 const player=record&&!collision?eventPlayer(record as EventDto):null
 const rawClip=record?.clip_url,clip=rawClip?(rawClip.startsWith('http')?rawClip:new URL(rawClip,import.meta.env.VITE_API_BASE_URL??window.location.origin).toString()):null
 const detailTitle=collision?'Player-contact event':record?`${record.event_type} incident`:'Incident details'
 const renderPerson=(title:string,p:ReturnType<typeof eventPlayer>|ReturnType<typeof collisionParticipantPlayer>|null)=><Card key={title}><p className="text-xs font-semibold uppercase text-slate-500">{title}</p><p className="mt-1 text-lg font-bold">{p?playerIdentityLabel(p):'Player'}</p></Card>
 return <><PageHeader title={detailTitle} crumbs={[{label:collision?'Collisions':'Events',to:collision?'/collisions':'/events'},{label:'Details'}]} /><div className="mb-4"><Disclaimer/></div>{q.isLoading?<Card><Skeleton/></Card>:q.isError?<ErrorState title="Unable to load record" text={normalizeApiError(q.error)} onRetry={()=>q.refetch()}/>:!record?<EmptyState title="Record unavailable"/>:<div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{collision?(people?<>{renderPerson('Player A',collisionParticipantPlayer(people[0]))}{renderPerson('Player B',collisionParticipantPlayer(people[1]))}</>:<Card><p>Participants are unavailable or incomplete; identities were not inferred.</p></Card>):renderPerson('Player',player)}<Card><h2 className="font-semibold">Risk and confidence</h2><dl className="mt-2 space-y-2 text-sm"><div>Risk: <RiskBadge level={record.risk}/></div><div>Risk score: {record.risk_score??'—'}</div><div>Model/event confidence: {record.confidence??'—'}</div><div>Recorded: {record.timestamp?new Date(record.timestamp).toLocaleString():'—'}</div></dl></Card><Card><h2 className="font-semibold">Event details</h2><dl className="mt-2 space-y-2 text-sm"><div>Type: {record.event_type}</div><div>Match: {eventMatchLabel((record as EventDto).match_name??null,record.match_session_id,new Map())}</div><div>Video time: {record.video_time_sec??'—'} sec</div><div>Frame: {record.frame_number??'—'}</div><div>Region: {record.region??'—'}</div><div>Side: {'side' in record?(record.side??'—'):'—'}</div>{!collision&&<div>Evidence: {(record as EventDto).note??'—'}</div>}<div>Resolved: {String(record.resolved??'—')}</div>{collision&&<><div>IoU: {(record as CollisionDto).iou??'—'}</div><div>Contact frames: {(record as CollisionDto).contact_frames}</div><div>Movement: {(record as CollisionDto).participants.map(p=>p.movement??'—').join(', ')}</div></>}</dl></Card><MedicalScan eventId={collision?null:n} collisionId={collision?n:null}/><Card className="md:col-span-2"><h2 className="font-semibold">Video clip</h2>{clip?<><video controls preload="metadata" className="mt-3 max-h-96 w-full rounded-lg bg-black" src={clip} onError={()=>setClipError(true)}>Video playback is not supported by this browser.</video>{clipError&&<p role="alert" className="mt-2 text-sm text-red-700">Clip playback failed. The signed clip may have expired or may be unavailable.<button className="ml-2 underline" onClick={()=>{setClipError(false);void q.refetch()}}>Request clip again</button></p>}</>:<p className="mt-2 text-sm text-slate-500">No signed clip URL is available for this record.</p>}</Card></div>}</>
}/* ---------- MEDICAL ASSESSMENT (Medical only; route-guarded) ---------- */
export function Assessment(){
  const {id}=useParams(),eventId=Number(id)
  return <><PageHeader title="Medical assessment" crumbs={[{label:'Events',to:'/events'},{label:'Incident',to:`/events/${id}`},{label:'Assessment'}]}/>
    <MedicalScan eventId={Number.isFinite(eventId)?eventId:null} collisionId={null}/></>
}

/* ---------- ALERTS ---------- */
export function Alerts(){
 const [pri,setPri]=useState('All'),[read,setRead]=useState('All'),[offset,setOffset]=useState(0),qc=useQueryClient()
 const query=useQuery({queryKey:['alerts',{pri,read,offset}],queryFn:()=>alerts.list({severity:pri==='All'?undefined:pri.toUpperCase(),unread:read==='Unread'?true:read==='Read'?false:undefined,limit:50,offset}),refetchInterval:30000})
 const mark=useMutation({mutationFn:(id:number)=>alerts.markRead(id),onSuccess:()=>qc.invalidateQueries({queryKey:['alerts']})})
 const rows=query.data??[]
 const subjects=useQueries({queries:rows.map(a=>({
  queryKey:a.injury_event_id!==null?['alert-identity','event',a.injury_event_id]:['alert-identity','collision',a.collision_event_id],
  queryFn:()=>a.injury_event_id!==null?events.get(a.injury_event_id):collisions.get(a.collision_event_id!),
  enabled:a.injury_event_id!==null||a.collision_event_id!==null,
 }))})
 const subjectLabel=(a:AlertDto,index:number)=>{
  const record=subjects[index]?.data
  if(a.injury_event_id!==null)return alertPlayerLabel(a,record as EventDto|undefined)
  if(a.collision_event_id!==null){
   return alertPlayerLabel(a,undefined,record as CollisionDto|undefined)
  }
  return alertPlayerLabel(a)
 }
 return <><PageHeader title="Alerts" crumbs={[{label:'Alerts'}]}/><Card className="!p-0"><div className="flex flex-wrap gap-2 border-b p-3">{['All','Critical','High','Medium','Low'].map(p=><button key={p} aria-pressed={pri===p} onClick={()=>{setPri(p);setOffset(0)}} className={`${btn} ${pri===p?'bg-[#0b1f3a] text-white':'border border-slate-300'}`}>{p}</button>)}<select aria-label="Read state" className={inp+' ml-auto w-auto'} value={read} onChange={e=>{setRead(e.target.value);setOffset(0)}}><option>All</option><option>Unread</option><option>Read</option></select></div>{query.isLoading?<div className="p-4"><Skeleton/></div>:query.isError?<ErrorState title="Unable to load alerts" text={normalizeApiError(query.error)} onRetry={()=>query.refetch()}/>:rows.length===0?<EmptyState title="No alerts" text="No alerts match the selected filters."/>:<div className="divide-y">{rows.map((a,i)=><div key={a.id} className="flex items-start justify-between gap-4 p-4"><div><div className="flex items-center gap-2"><b>{subjectLabel(a,i)} — {a.severity??'Unrated'}</b><RiskBadge level={a.severity}/>{!a.is_read&&<Badge tone="amber">Unread</Badge>}</div><p className="mt-1 text-sm">{safeAlertText(a.message)}</p><p className="mt-1 text-xs text-slate-500">{safeAlertText(a.title??a.alert_type)} · {a.created_at??a.sent_at??'Time unavailable'}</p></div>{!a.is_read&&<button className={btnS} disabled={mark.isPending} onClick={()=>mark.mutate(a.id)}>Mark read</button>}</div>)}</div>}{rows.length>0&&<div className="flex items-center justify-between border-t p-3 text-sm"><span>Showing {offset+1}–{offset+rows.length}</span><div><button className={btnS} disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-50))}>Previous</button> <button className={btnS} disabled={rows.length<50} onClick={()=>setOffset(offset+50)}>Next</button></div></div>}{mark.isError&&<p role="alert" className="p-3 text-sm text-red-700">{normalizeApiError(mark.error)}</p>}</Card></>
}
/* ---------- ADMIN / SETTINGS / HELP / ERRORS ---------- */
export function System(){
  const q=useQuery({queryKey:['health'],queryFn:()=>system.health()})
  return <><PageHeader title="System status" crumbs={[{label:'System'}]}/>{q.isLoading?<Card><Skeleton/></Card>:q.isError?<ErrorState title="Unable to load system health" text={normalizeApiError(q.error)} onRetry={()=>q.refetch()}/>:<div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{Object.entries(q.data??{}).map(([key,value])=><Card key={key}><p className="text-xs text-slate-500">{key}</p><div className="mt-2"><Badge>{value}</Badge></div></Card>)}</div>}</>
}
export function Settings(){
  const {user}=useAuth(); const [tab,setTab]=useState('Profile')
  const [currentPassword,setCurrentPassword]=useState('')
  const [newPassword,setNewPassword]=useState('')
  const [confirmPassword,setConfirmPassword]=useState('')
  const [passwordFormError,setPasswordFormError]=useState('')
  const passwordChange=useMutation({
    mutationFn:(body:PasswordChangeBody)=>session.changePassword(body),
    onSuccess:()=>{setCurrentPassword('');setNewPassword('');setConfirmPassword('');setPasswordFormError('')},
  })
  return <><PageHeader title="Settings" crumbs={[{label:'Settings'}]}/><div role="tablist" className="mb-4 flex gap-1 border-b">{['Profile','Preferences','Notifications','Security'].map(t=><button key={t} role="tab" aria-selected={tab===t} onClick={()=>setTab(t)} className={`px-4 py-2 text-sm ${tab===t?'border-b-2 border-[#0b1f3a] font-semibold':'text-slate-500'}`}>{t}</button>)}</div>
    <Card className="max-w-xl space-y-3">{tab==='Profile'&&<><Field label="Name"><input className={inp} defaultValue={user?.name}/></Field><Field label="Role"><input className={inp} value={user?.role} readOnly/></Field></>}
      {tab==='Preferences'&&<Field label="Time format"><select className={inp}><option>24-hour</option><option>12-hour</option></select></Field>}
      {tab==='Notifications'&&['High-risk alerts','Medium-risk alerts','Low-risk alerts'].map(n=><label key={n} className="flex items-center gap-2 text-sm"><input type="checkbox" defaultChecked/>{n}</label>)}
      {tab==='Security'&&<form className="space-y-3" onSubmit={event=>{event.preventDefault();setPasswordFormError('');passwordChange.reset();if(newPassword!==confirmPassword){setPasswordFormError('New passwords do not match.');return}if(newPassword.length<8){setPasswordFormError('New password must be at least 8 characters.');return}passwordChange.mutate({current_password:currentPassword,new_password:newPassword})}}>
        <Field label="Current password"><input type="password" autoComplete="current-password" required className={inp} value={currentPassword} onChange={event=>setCurrentPassword(event.target.value)}/></Field>
        <Field label="New password"><input type="password" autoComplete="new-password" required minLength={8} className={inp} value={newPassword} onChange={event=>setNewPassword(event.target.value)}/></Field>
        <Field label="Confirm new password"><input type="password" autoComplete="new-password" required minLength={8} className={inp} value={confirmPassword} onChange={event=>setConfirmPassword(event.target.value)}/></Field>
        <p className="text-xs text-slate-600">Your current password is required. Forgotten passwords must be recovered by a system administrator; no email reset is configured.</p>
        {passwordFormError&&<p role="alert" className="text-sm text-red-700">{passwordFormError}</p>}
        {passwordChange.isError&&<p role="alert" className="text-sm text-red-700">{normalizeApiError(passwordChange.error)}</p>}
        {passwordChange.isSuccess&&<p role="status" className="text-sm text-green-700">{passwordChange.data.message}</p>}
        <button className={btnP} disabled={passwordChange.isPending}>{passwordChange.isPending?'Changing password…':'Change password'}</button>
      </form>}
      {tab!=='Security'&&<><p className="text-xs text-amber-800">Profile, preference, and notification updates are not available in the backend.</p><button className={btnP} disabled>Save changes unavailable</button></>}</Card></>
}
export function Help(){
  return <><PageHeader title="Help &amp; about" crumbs={[{label:'Help'}]}/><div className="grid gap-4 lg:grid-cols-2"><Card><h2 className="font-semibold">About AthleteGuard</h2><p className="mt-2 text-sm text-slate-600">AI-Powered Football Injury Detection &amp; Medical Decision Support. Match video is analyzed for player tracking, identity, collisions and abnormal movement; incidents are routed to staff for review.</p></Card>
    <Card><h2 className="font-semibold">Roles</h2><ul className="mt-2 list-disc pl-5 text-sm text-slate-600"><li><b>Admin</b>: teams, players, matches, users, system. No clinical notes.</li><li><b>Medical</b>: review incidents and record clinical assessments.</li><li><b>Coach</b>: own team only; event status, no clinical notes.</li><li><b>Player</b>: own profile, team matches, personal incidents, assessments, and alerts.</li></ul></Card>
    <div className="lg:col-span-2"><Disclaimer/></div><Card><h2 className="font-semibold">Support</h2><p className="mt-2 text-sm text-slate-600">Contact your system administrator. Support contact details are not configured.</p></Card></div></>
}
export function ErrorPage({kind}:{kind:'403'|'404'|'network'|'generic'}){
  const {user}=useAuth(); const I={'403':Lock,'404':FileQuestion,network:WifiOff,generic:ServerCrash}[kind]
  const T={'403':['Access denied','You don’t have permission to access this page.'],'404':['Page not found','The page you requested does not exist.'],network:['Network unavailable','Check your connection and try again.'],generic:['Something went wrong','An unexpected error occurred. Please try again.']}[kind]
  return <div className="grid place-items-center py-20 text-center"><I className="text-slate-400" size={48}/><h1 className="mt-3 text-2xl font-bold">{T[0]}</h1><p className="mt-1 text-slate-500">{T[1]}</p><Link className={btnP+' mt-5'} to={user?home[user.role]:'/login'}>Back to dashboard</Link></div>
}
