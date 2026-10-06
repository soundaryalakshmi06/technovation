import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, Inbox, AlertTriangle, X, CheckCircle2, ShieldAlert } from 'lucide-react'
import type { RiskLevel } from './types'
export const DISCLAIMER_1='AI-suggested screening categories, NOT a confirmed diagnosis.'
export const DISCLAIMER_2='This system is intended for screening and decision support only. Final clinical assessment must be performed by a qualified medical professional.'
export const btn='inline-flex items-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition disabled:opacity-50 disabled:cursor-not-allowed'
export const btnP=`${btn} bg-[#0b1f3a] text-white hover:bg-[#16325c]`
export const btnS=`${btn} border border-slate-300 bg-white text-slate-700 hover:bg-slate-50`
export const inp='w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm'
export const Card=({children,className=''}:{children:ReactNode;className?:string})=><div className={`rounded-2xl border border-slate-200 bg-white p-5 shadow-sm ${className}`}>{children}</div>
const tones={green:'bg-green-100 text-green-800',amber:'bg-amber-100 text-amber-800',red:'bg-red-100 text-red-800',critical:'bg-red-200 text-red-950',blue:'bg-blue-100 text-blue-800',gray:'bg-slate-100 text-slate-700'}
export const Badge=({tone='gray',children}:{tone?:keyof typeof tones;children:ReactNode})=><span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold ${tones[tone]}`}>{children}</span>
export const RiskBadge=({level}:{level:RiskLevel|null})=>level?<Badge tone={level==='CRITICAL'?'critical':level==='HIGH'?'red':level==='MEDIUM'?'amber':'green'}>{(level==='HIGH'||level==='CRITICAL')&&<ShieldAlert size={12}/>}{level}</Badge>:<Badge>Unrated</Badge>
export const EmptyState=({title,text,action}:{title:string;text?:string;action?:ReactNode})=><div className="flex flex-col items-center gap-2 px-4 py-10 text-center"><Inbox className="text-slate-400" size={32}/><p className="font-semibold">{title}</p>{text&&<p className="max-w-md text-sm text-slate-500">{text}</p>}{action}</div>
export const ErrorState=({title,text,onRetry}:{title:string;text?:string;onRetry?:()=>void})=><div className="flex flex-col items-center gap-2 px-4 py-10 text-center"><AlertTriangle className="text-red-500" size={32}/><p className="font-semibold">{title}</p>{text&&<p className="text-sm text-slate-500">{text}</p>}{onRetry&&<button className={btnS} onClick={onRetry}>Try again</button>}</div>
export const Skeleton=({className='h-4 w-full'}:{className?:string})=><div className={`animate-pulse rounded bg-slate-200 ${className}`}/>
export const Disclaimer=()=><div role="note" className="rounded-xl border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900"><p className="font-semibold">{DISCLAIMER_1}</p><p className="mt-1">{DISCLAIMER_2}</p></div>
export const Na=({v}:{v?:string|number|null})=><>{v===null||v===undefined||v===''?'—':v}</>
export function PageHeader({title,crumbs=[],actions,back=true}:{title:string;crumbs?:{label:string;to?:string}[];actions?:ReactNode;back?:boolean}){
  const nav=useNavigate()
  return <div className="mb-5">
    <nav aria-label="Breadcrumb" className="mb-1 flex flex-wrap items-center gap-1 text-xs text-slate-500"><Link to="/" className="hover:underline">Home</Link>{crumbs.map(c=><span key={c.label} className="flex gap-1">/ {c.to?<Link to={c.to} className="hover:underline">{c.label}</Link>:<span aria-current="page">{c.label}</span>}</span>)}</nav>
    <div className="flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2">{back&&<button aria-label="Back" className={btnS+' !px-2'} onClick={()=>nav(-1)}><ArrowLeft size={16}/></button>}<h1 className="text-2xl font-bold">{title}</h1></div><div className="flex flex-wrap gap-2">{actions}</div></div></div>
}
export function Modal({open,title,onClose,children}:{open:boolean;title:string;onClose:()=>void;children:ReactNode}){
  if(!open)return null
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4" role="dialog" aria-modal="true" aria-label={title}><div className="max-h-[90vh] w-full max-w-lg overflow-auto rounded-2xl bg-white p-6 shadow-xl"><div className="mb-4 flex items-center justify-between"><h2 className="text-lg font-semibold">{title}</h2><button aria-label="Close" onClick={onClose}><X size={18}/></button></div>{children}</div></div>
}
export function Confirm({open,title,text,confirmLabel,onConfirm,onClose}:{open:boolean;title:string;text:string;confirmLabel:string;onConfirm:()=>void;onClose:()=>void}){
  return <Modal open={open} title={title} onClose={onClose}><p className="text-sm text-slate-600">{text}</p><div className="mt-5 flex justify-end gap-2"><button className={btnS} onClick={onClose}>Cancel</button><button className={`${btn} bg-red-600 text-white hover:bg-red-700`} onClick={()=>{onConfirm();onClose()}}>{confirmLabel}</button></div></Modal>
}
type T={id:number;msg:string;err?:boolean}
const TC=createContext<(m:string,err?:boolean)=>void>(()=>{})
export const useToast=()=>useContext(TC)
export function ToastProvider({children}:{children:ReactNode}){
  const [ts,setTs]=useState<T[]>([])
  const push=useCallback((msg:string,err?:boolean)=>{const id=Date.now()+Math.random();setTs(t=>[...t,{id,msg,err}]);setTimeout(()=>setTs(t=>t.filter(x=>x.id!==id)),4000)},[])
  return <TC.Provider value={push}>{children}<div className="fixed bottom-4 right-4 z-[60] space-y-2" aria-live="polite">{ts.map(t=><div key={t.id} className={`flex items-center gap-2 rounded-lg px-4 py-3 text-sm text-white shadow-lg ${t.err?'bg-red-600':'bg-[#0b1f3a]'}`}>{!t.err&&<CheckCircle2 size={16}/>}{t.msg}</div>)}</div></TC.Provider>
}
