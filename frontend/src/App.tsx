import { useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AppProvider } from './lib/store'
import { Layout } from './components/ui'
import Login from './pages/Login'
import HealthPage from './pages/Health'
import ProductsPage from './pages/Products'
import RegistryPage from './pages/Registry'
import ImpactPage from './pages/Impact'
import IncidentsPage from './pages/Incidents'
import SimulatorPage from './pages/Simulator'
import AcceptancePage from './pages/Acceptance'
import DemandPage from './pages/Demand'

type User = { display_name: string; role: string }

function readUser(): User | null {
  const raw = localStorage.getItem('park-slo-user')
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    return null
  }
}

export default function App() {
  const [user, setUser] = useState<User | null>(readUser())

  if (!user) return <Login onLogin={setUser} />

  return (
    <AppProvider>
      <BrowserRouter>
        <Layout user={user}>
          <Routes>
            <Route path="/" element={<HealthPage />} />
            <Route path="/products" element={<ProductsPage />} />
            <Route path="/registry" element={<RegistryPage />} />
            <Route path="/impact" element={<ImpactPage />} />
            <Route path="/incidents" element={<IncidentsPage />} />
            <Route path="/simulator" element={<SimulatorPage />} />
            <Route path="/acceptance" element={<AcceptancePage />} />
            <Route path="/demand" element={<DemandPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Layout>
      </BrowserRouter>
    </AppProvider>
  )
}
