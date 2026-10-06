import { type ReactNode, useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider, home, useAuth } from './auth'
import { ToastProvider } from './ui'
import Layout from './Layout'
import type { Role } from './types'
import * as P from './pages'
const qc=new QueryClient({defaultOptions:{queries:{retry:false,refetchOnWindowFocus:false}}})
const G=({roles,children}:{roles:Role[];children:ReactNode})=>{const {user,loading}=useAuth();if(loading)return null;if(!user)return <Navigate to="/login" replace/>;return roles.includes(user.role)?<>{children}</>:<P.ErrorPage kind="403"/>}
const RoleHome=()=>{const {user}=useAuth();return user?<Navigate to={home[user.role]} replace/>:null}
const ALL:Role[]=['ADMIN','MEDICAL','COACH','PLAYER']
function ClearUserQueries(){useEffect(()=>{const clear=()=>qc.clear();window.addEventListener('athleteguard:logout',clear);window.addEventListener('athleteguard:unauthorized',clear);return()=>{window.removeEventListener('athleteguard:logout',clear);window.removeEventListener('athleteguard:unauthorized',clear)}},[]);return null}
export default function App(){return <QueryClientProvider client={qc}><BrowserRouter><AuthProvider><ClearUserQueries/><ToastProvider><Routes>
  <Route path="/login" element={<P.Login/>}/>
  <Route element={<Layout/>}>
    <Route index element={<RoleHome/>}/>
    <Route path="/admin/dashboard" element={<G roles={['ADMIN']}><P.Dashboard role="ADMIN"/></G>}/>
    <Route path="/coach/dashboard" element={<G roles={['COACH']}><P.Dashboard role="COACH"/></G>}/>
    <Route path="/medical/dashboard" element={<G roles={['MEDICAL']}><P.Dashboard role="MEDICAL"/></G>}/>
    <Route path="/player/dashboard" element={<G roles={['PLAYER']}><P.Dashboard role="PLAYER"/></G>}/>
    <Route path="/teams" element={<G roles={['ADMIN','COACH']}><P.ListPage k="teams"/></G>}/>
    <Route path="/teams/:id" element={<G roles={['ADMIN','COACH']}><P.Detail k="teams"/></G>}/>
    <Route path="/players" element={<G roles={ALL}><P.ListPage k="players"/></G>}/>
    <Route path="/players/:id" element={<G roles={ALL}><P.Detail k="players"/></G>}/>
    <Route path="/matches" element={<G roles={ALL}><P.ListPage k="matches"/></G>}/>
    <Route path="/matches/:id" element={<G roles={ALL}><P.MatchDetail/></G>}/>
    <Route path="/matches/:id/monitor" element={<G roles={['ADMIN','MEDICAL','COACH']}><P.Monitor/></G>}/>
    <Route path="/events" element={<G roles={ALL}><P.ListPage k="events"/></G>}/>
    <Route path="/events/:id" element={<G roles={ALL}><P.EventDetail/></G>}/>
    <Route path="/events/:id/assessment" element={<G roles={['MEDICAL']}><P.Assessment/></G>}/>
    <Route path="/collisions" element={<G roles={ALL}><P.ListPage k="collisions"/></G>}/>
    <Route path="/collisions/:id" element={<G roles={ALL}><P.EventDetail collision/></G>}/>
    <Route path="/alerts" element={<G roles={ALL}><P.Alerts/></G>}/>
    <Route path="/reports" element={<G roles={['ADMIN','MEDICAL']}><P.Reports/></G>}/>
    <Route path="/admin/users" element={<G roles={['ADMIN']}><P.ListPage k="users"/></G>}/>
    <Route path="/admin/system" element={<G roles={['ADMIN']}><P.System/></G>}/>
    <Route path="/settings" element={<P.Settings/>}/>
    <Route path="/help" element={<P.Help/>}/>
    <Route path="/offline" element={<P.ErrorPage kind="network"/>}/>
    <Route path="/error" element={<P.ErrorPage kind="generic"/>}/>
    <Route path="*" element={<P.ErrorPage kind="404"/>}/>
  </Route></Routes></ToastProvider></AuthProvider></BrowserRouter></QueryClientProvider>}
