import { useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { NavLink, Navigate, Outlet, useNavigate } from 'react-router-dom'
import { LayoutDashboard, Users, Shield, CalendarDays, Activity, Zap, Bell, FileText, UserCog, Server, Settings, Menu, PanelLeft, Search, LogOut, HelpCircle, ChevronDown, type LucideIcon } from 'lucide-react'
import { useAuth, home } from './auth'
import type { Role } from './types'
import { Badge, RiskBadge } from './ui'
import { alerts } from './api/services'
import { isConnected } from './api/client'
import { safeAlertText } from './api/mappers'
type N={label:string;to:string;icon:LucideIcon}
const common=(r:Role):N[]=>[{label:'Players',to:'/players',icon:Users},{label:'Matches',to:'/matches',icon:CalendarDays},{label:'Events',to:'/events',icon:Activity},{label:'Collisions',to:'/collisions',icon:Zap},{label:'Alerts',to:'/alerts',icon:Bell}].concat(r==='COACH'?[]:[])
export const navFor=(r:Role):N[]=>{
  const dash:N={label:'Dashboard',to:home[r],icon:LayoutDashboard}
  const set:N={label:'Settings',to:'/settings',icon:Settings}
  const rep:N={label:'Reports',to:'/reports',icon:FileText}
  if(r==='ADMIN')return [dash,{label:'Teams',to:'/teams',icon:Shield},...common(r),rep,{label:'Users',to:'/admin/users',icon:UserCog},{label:'System',to:'/admin/system',icon:Server},set]
  if(r==='MEDICAL')return [dash,...common(r),rep,set]
  if(r==='COACH')return [dash,{label:'My Team',to:'/teams',icon:Shield},...common(r),set]
  return [dash,{label:'My Profile',to:'/players/me',icon:Users},{label:'Matches',to:'/matches',icon:CalendarDays},{label:'My Incidents',to:'/events',icon:Activity},{label:'Collisions',to:'/collisions',icon:Zap},{label:'Alerts',to:'/alerts',icon:Bell},set]
}
export default function Layout(){
  const {user,logout,loading}=useAuth(); const nav=useNavigate()
  const [collapsed,setCollapsed]=useState(false); const [drawer,setDrawer]=useState(false)
  const [bell,setBell]=useState(false); const [menu,setMenu]=useState(false); const [q,setQ]=useState('')
  const unreadAlerts=useQuery({
    queryKey:['alerts','unread-count'],
    enabled:isConnected&&!!user&&!user.dev,
    refetchInterval:30000,
    queryFn:()=>alerts.listAll({unread:true}),
  })
  if(loading)return null
  if(!user)return <Navigate to="/login" replace/>
  const items=navFor(user.role).map(item=>item.to==='/players/me'&&user.playerId?{...item,to:`/players/${user.playerId}`} : item)
  const unreadCount=unreadAlerts.data?.length
  const unreadPreview=[...(unreadAlerts.data??[])].sort((a,b)=>{
    const priority:Record<string,number>={CRITICAL:0,HIGH:1,MEDIUM:2,LOW:3}
    return (priority[a.severity?.toUpperCase()??'']??4)-(priority[b.severity?.toUpperCase()??'']??4)
      ||(Date.parse(b.created_at??'')||0)-(Date.parse(a.created_at??'')||0)
  }).slice(0,5)
  const search=(e:FormEvent)=>{e.preventDefault();nav(`/events?q=${encodeURIComponent(q)}`)}
  const side=(mobile:boolean)=><nav aria-label="Main" className="flex h-full flex-col bg-[#0b1f3a] text-slate-200">
    <div className="flex h-16 items-center gap-2 px-4 text-white"><Shield className="text-sky-400" size={26}/>{(mobile||!collapsed)&&<div><p className="font-bold leading-tight">AthleteGuard</p><p className="text-[10px] text-slate-400">Injury Detection</p></div>}</div>
    <ul className="flex-1 space-y-1 overflow-y-auto px-2 py-2">{items.map(i=><li key={i.to}><NavLink to={i.to} onClick={()=>setDrawer(false)} title={i.label} className={({isActive})=>`flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${isActive?'bg-sky-500/20 text-white':'hover:bg-white/10'}`}><i.icon size={18}/>{(mobile||!collapsed)&&i.label}</NavLink></li>)}</ul>
    <NavLink to="/help" className="m-2 flex items-center gap-3 rounded-lg px-3 py-2 text-sm hover:bg-white/10"><HelpCircle size={18}/>{(mobile||!collapsed)&&'Help'}</NavLink></nav>
  return <div className="flex min-h-screen">
    <aside className={`hidden shrink-0 lg:block ${collapsed?'w-16':'w-60'}`}><div className="sticky top-0 h-screen">{side(false)}</div></aside>
    {drawer&&<div className="fixed inset-0 z-40 lg:hidden"><div className="absolute inset-0 bg-slate-900/50" onClick={()=>setDrawer(false)}/><div className="relative h-full w-64">{side(true)}</div></div>}
    <div className="flex min-w-0 flex-1 flex-col">
      <header className="sticky top-0 z-30 flex h-16 items-center gap-3 border-b border-slate-200 bg-white px-4">
        <button aria-label="Open menu" className="lg:hidden" onClick={()=>setDrawer(true)}><Menu/></button>
        <button aria-label="Collapse sidebar" className="hidden lg:block" onClick={()=>setCollapsed(c=>!c)}><PanelLeft/></button>
        <form onSubmit={search} className="relative max-w-md flex-1" role="search"><Search size={16} className="absolute left-3 top-2.5 text-slate-400"/><input aria-label="Search events, players" value={q} onChange={e=>setQ(e.target.value)} placeholder="Search players, events…" className="w-full rounded-lg border border-slate-300 py-2 pl-9 pr-3 text-sm"/></form>
        <div className="relative ml-auto"><button aria-label={`Notifications, ${unreadCount??'unknown'} unread`} onClick={()=>{setBell(b=>!b);setMenu(false)}} className="relative rounded-lg p-2 hover:bg-slate-100"><Bell size={20}/>{typeof unreadCount==='number'&&unreadCount>0&&<span className="absolute -right-1 -top-1 rounded-full bg-red-600 px-1.5 py-0.5 text-[10px] font-bold text-white">{unreadCount}</span>}</button>
          {bell&&<div className="absolute right-0 mt-2 w-80 rounded-xl border border-slate-200 bg-white p-4 text-sm shadow-lg"><p className="font-semibold">Notifications</p>{unreadAlerts.isError?<p className="py-4 text-center text-slate-500">Unread alerts could not be loaded.</p>:unreadCount===undefined?<p className="py-4 text-center text-slate-500">{isConnected?'Loading unread alerts…':'Backend not connected'}</p>:unreadCount===0?<p className="py-4 text-center text-slate-500">No unread alerts</p>:<>
            <p className="py-2 text-xs text-slate-500">{unreadCount} unread alert{unreadCount===1?'':'s'}</p>
            <div className="max-h-72 divide-y overflow-y-auto">{unreadPreview.map(alert=><button key={alert.id} className={`block w-full px-1 py-3 text-left hover:bg-slate-50 ${alert.severity==='CRITICAL'?'border-l-4 border-red-600 pl-2':''}`} onClick={()=>{setBell(false);nav('/alerts')}}>
              <span className="flex items-center justify-between gap-2"><span className="font-medium">{safeAlertText(alert.title??alert.alert_type)}</span><RiskBadge level={alert.severity}/></span>
              <span className="mt-1 block line-clamp-2 text-xs text-slate-500">{safeAlertText(alert.message)}</span>
            </button>)}</div>
          </>}<button className="mt-3 text-blue-700 underline" onClick={()=>{setBell(false);nav('/alerts')}}>View all alerts</button></div>}</div>
        <div className="relative"><button onClick={()=>{setMenu(m=>!m);setBell(false)}} className="flex items-center gap-2 rounded-lg p-1.5 hover:bg-slate-100" aria-haspopup="menu" aria-expanded={menu}><span className="grid h-8 w-8 place-items-center rounded-full bg-[#0b1f3a] text-xs font-bold text-white">{user.name[0].toUpperCase()}</span><span className="hidden text-left text-xs sm:block"><b className="block">{user.name}</b>{user.role}</span><ChevronDown size={14}/></button>
          {menu&&<div role="menu" className="absolute right-0 mt-2 w-44 rounded-xl border border-slate-200 bg-white p-1 text-sm shadow-lg"><button role="menuitem" className="block w-full rounded px-3 py-2 text-left hover:bg-slate-100" onClick={()=>{setMenu(false);nav('/settings')}}>Profile & settings</button><button role="menuitem" className="flex w-full items-center gap-2 rounded px-3 py-2 text-left text-red-700 hover:bg-slate-100" onClick={()=>{logout();nav('/login',{replace:true})}}><LogOut size={14}/>Log out</button></div>}</div>
      </header>
      {user.dev&&<div className="bg-amber-100 px-4 py-1.5 text-center text-xs font-semibold text-amber-900">DEV PREVIEW: not authenticated, no backend data. <Badge tone="amber">DEV DATA</Badge></div>}
      <main className="flex-1 p-4 md:p-6"><Outlet/></main></div></div>
}
