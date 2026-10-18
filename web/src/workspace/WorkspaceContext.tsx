import { createContext, useContext, type ReactNode } from 'react';
import { ALPHA, TRAINING, get, health, type DataSummary, type MiningTask, type Health, type Row } from './api';
import { useResource } from './hooks';

function useOverview() {
  const service = useResource(signal => health(signal), [], 30000);
  const summary = useResource(signal => get<DataSummary>(`${ALPHA}/data-summary`, undefined, signal), [], 0);
  const tasks = useResource(signal => get<{ tasks: MiningTask[]; total: number }>(`${ALPHA}/tasks/history`, { limit: 200 }, signal), [], 15000);
  const stats = useResource(signal => get<Row>(`${ALPHA}/stats`, undefined, signal), [], 30000);
  const material = useResource(signal => get<Row>(`${TRAINING}/rd-mined/materialize/status`, undefined, signal), [], 30000);
  return { service, summary, tasks, stats, material, refresh() { service.refresh(); summary.refresh(); tasks.refresh(); stats.refresh(); material.refresh(); } };
}
const WorkspaceContext = createContext<ReturnType<typeof useOverview>>(null!);
export function WorkspaceProvider({ children }: { children: ReactNode }) { const overview = useOverview(); return <WorkspaceContext.Provider value={overview}>{children}</WorkspaceContext.Provider>; }
export const useWorkspace = () => useContext(WorkspaceContext);
