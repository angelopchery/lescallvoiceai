import { useNavigate } from 'react-router-dom'
import { Compass } from 'lucide-react'
import { Card, Button, EmptyState } from '../components/ui'

export default function Placeholder({ title = 'Page not found', sub }: { title?: string; sub?: string }) {
  const nav = useNavigate()
  return (
    <Card className="p-6">
      <EmptyState
        icon={<Compass size={22} />}
        title={title}
        hint={sub ?? 'The page you were looking for doesn’t exist or has moved.'}
        action={<Button onClick={() => nav('/')}>Back to home</Button>}
      />
    </Card>
  )
}
