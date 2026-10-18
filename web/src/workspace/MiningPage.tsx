import { useNavigate } from 'react-router-dom';
import { Button } from 'antd';
import { Sparkles } from 'lucide-react';
import MiningForm from './MiningForm';
import { Page } from './ui';

export default function MiningPage() {
  const navigate = useNavigate();
  return <Page className="mining-config-page" eyebrow={<><Sparkles size={14} />挖掘默认值 · 数据窗口 · 质量门禁</>} title="参数配置" description="保存后，首页新建的挖掘任务会自动使用这组参数。" actions={<Button onClick={() => navigate('/tasks')}>查看任务中心</Button>}>
    <MiningForm full configOnly />
  </Page>;
}
