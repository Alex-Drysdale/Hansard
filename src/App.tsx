import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import DebateView from './pages/DebateView'
import TopicIntelligence from './pages/TopicIntelligence'
import MemberTracker from './pages/MemberTracker'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="debate/:id" element={<DebateView />} />
          <Route path="topics" element={<TopicIntelligence />} />
          <Route path="tracker" element={<MemberTracker />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
