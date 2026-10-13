import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { request } from '@/hermesTransport';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/Table';
import { useHeaderRefresh } from '@/hooks/useHeaderRefresh';
import { SectionCard } from './config/components/SectionCard';

type Feature = 'providers' | 'auth' | 'models' | 'quota' | 'logs' | 'plugins';
type Projection = {
  available: boolean;
  withheld?: number | null;
  items?: Array<Record<string, string | number | null>>;
};
const columns: Partial<Record<Feature, string[]>> = {
  providers: ['provider', 'priority', 'credentials', 'disabled_credentials'],
  models: ['model', 'context_length', 'max_completion_tokens'],
  logs: ['severity'],
};
export function HermesInventory({ feature }: { feature: Feature }) {
  const { t } = useTranslation();
  const [data, setData] = useState<Projection | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState('ordinal');
  const supported = Object.prototype.hasOwnProperty.call(columns, feature);
  useHeaderRefresh(() => { if (supported) setRefresh(value => value + 1); });
  useEffect(() => {
    let active = true;
    setData(null);
    if (!supported) return;
    request(`${feature}.read`).then(value => {
      if (active) setData(value as Projection);
    }).catch(() => { if (active) setData({ available: false }); });
    return () => { active = false; };
  }, [feature, refresh, supported]);
  const fields = columns[feature] ?? [];
  const rows = (data?.items ?? []).filter(row => fields.some(field =>
    String(row[field] ?? '').toLowerCase().includes(filter.toLowerCase())))
    .sort((a, b) => typeof a[sort] === 'number' && typeof b[sort] === 'number'
      ? Number(a[sort]) - Number(b[sort]) : String(a[sort] ?? '').localeCompare(String(b[sort] ?? '')));
  return <SectionCard title={t(feature)} description={t(`${feature}_note`)}>
    {supported && <Button disabled={data === null} onClick={() => setRefresh(value => value + 1)}>{t('refresh')}</Button>}
    <p role="status">{!supported ? t(`${feature}_note`) : data === null ? t('loading') :
      !data.available ? t('unavailable') : t('entries', { count: data.items?.length ?? 0 })}</p>
    {data?.available && <>
      <Input label={t('hermes_filter')} value={filter} onChange={event => setFilter(event.target.value)} />
      <Table aria-label={t(feature)}>
        <TableHeader><TableRow><TableHead scope="col">{t('entry')}</TableHead>
          {fields.map(field => <TableHead scope="col" key={field} aria-sort={sort === field ? 'ascending' : 'none'}>
            <Button variant="ghost" onClick={() => setSort(field)}>{t(field)}</Button>
          </TableHead>)}</TableRow></TableHeader>
        <TableBody>{rows.map(row => <TableRow key={row.ordinal}>
          <TableCell>{row.ordinal}</TableCell>{fields.map(field => <TableCell key={field}>
            {row[field] ?? t('unknown')}</TableCell>)}
        </TableRow>)}</TableBody>
      </Table>
      <p>{t('entries', { count: rows.length })}</p>
    </>}
    {feature === 'models' && data?.available && <p>{t('withheld', { count: data.withheld ?? 0 })}</p>}
    <p id={`disabled-${feature}`}>{t('disabled')}</p>
    <Button disabled aria-describedby={`disabled-${feature}`}>
      {t(feature === 'logs' ? 'clear' : feature === 'quota' ? 'quota_refresh' :
        feature === 'models' ? 'aliases' : 'mutations')}
    </Button>
  </SectionCard>;
}
