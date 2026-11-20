import { useState } from 'react';
import { api } from '../api/client';
import type { ChartOut } from '../api/types';
import { useAsync, useRememberScrollX } from '../lib/hooks';
import { Link, enc } from '../lib/router';
import { SectionTitle, cx } from './ui';

/** «Чарты и жанры»: the charts of countries (cards with a collage of their first covers, the countries of Europe as
 * chips) and genres (tiles). A tap opens the chart's page (`pages/Chart.tsx`): in its order, shuffled or as a wave.
 * On the Home screen of a browser and above the recent searches (inside the app too). Nothing while it loads. */
export function ChartsSection() {
  const res = useAsync((s) => api.charts(s), []);
  const [allGenres, setAllGenres] = useState(false);
  const row = useRememberScrollX('charts:main');
  const charts = res.data?.charts ?? [];
  if (!charts.length) return null;
  const of = (g: ChartOut['group']) => charts.filter((c) => c.group === g);
  const genres = [...of('ru_genres'), ...of('genres')];
  const shown = allGenres ? genres : genres.slice(0, GENRES_SHOWN);

  return (
    <>
      <section aria-label="Чарты">
        <SectionTitle>Чарты</SectionTitle>
        <div ref={row} className="no-scrollbar flex gap-3 overflow-x-auto px-4 pb-1">
          {of('main').map((c, i) => (
            <Link key={c.id} to={`/chart/${enc(c.id)}`} className="w-36 shrink-0" aria-label={c.name}>
              <Collage chart={c} className="w-36" background={tone(i * 5 + 1)} />
              <div className="mt-1.5 truncate text-sm font-semibold">{c.title}</div>
              <div className="truncate text-xs text-muted">{c.source}</div>
            </Link>
          ))}
        </div>
        <div className="no-scrollbar flex gap-2 overflow-x-auto px-4 pt-3" aria-label="Страны Европы">
          {of('europe').map((c) => (
            <Link key={c.id} to={`/chart/${enc(c.id)}`} aria-label={c.name}
              className="flex min-h-9 shrink-0 items-center whitespace-nowrap rounded-full bg-raised px-3.5 text-sm font-semibold transition hover:bg-hover">
              {c.title}
            </Link>
          ))}
        </div>
      </section>
      <section aria-label="Жанры">
        <SectionTitle>Жанры</SectionTitle>
        <div className="grid grid-cols-2 gap-2 px-4 sm:grid-cols-3 lg:grid-cols-4">
          {shown.map((c, i) => (
            <Link key={c.id} to={`/chart/${enc(c.id)}`} aria-label={c.name} style={{ background: tone(i) }}
              className="flex min-h-14 items-end rounded-lg px-3 py-2 text-[15px] font-bold leading-tight text-white shadow-sm transition hover:brightness-110">
              {c.title}
            </Link>
          ))}
        </div>
        {genres.length > GENRES_SHOWN && (
          <div className="px-4 pt-2">
            <button type="button" className="min-h-11 text-sm font-semibold text-accent" aria-expanded={allGenres}
              onClick={() => setAllGenres((v) => !v)}>
              {allGenres ? 'Свернуть' : `Все жанры (${genres.length})`}
            </button>
          </div>
        )}
      </section>
    </>
  );
}

const GENRES_SHOWN = 8;

/** The chart's picture: its first 4 covers in a square (one cover alone when it has fewer), else its colour with the
 * title. */
export function Collage({ chart, className, background }: { chart: ChartOut; className?: string; background?: string }) {
  const covers = chart.covers;
  return (
    <div className={cx('relative aspect-square overflow-hidden rounded-lg bg-raised', className)}
      style={covers.length ? undefined : { background: background ?? gradient(chart.id) }}>
      {covers.length >= 4 ? (
        <div className="grid h-full w-full grid-cols-2 grid-rows-2">
          {covers.slice(0, 4).map((src) => (
            <img key={src} src={src} alt="" loading="lazy" referrerPolicy="no-referrer" className="h-full w-full object-cover" />
          ))}
        </div>
      ) : covers.length ? (
        <img src={covers[0]} alt="" loading="lazy" referrerPolicy="no-referrer" className="h-full w-full object-cover" />
      ) : (
        <span className="absolute inset-x-3 bottom-2.5 text-lg font-extrabold leading-tight text-white">{chart.title}</span>
      )}
    </div>
  );
}

/** Twelve clearly different colours: tiles take them in turn (neighbours never match), a chart's picture by its id. */
const TONES = [
  ['#e0245e', '#7a1036'], ['#1d9bf0', '#0d4a7a'], ['#f59e0b', '#8a4a06'], ['#10b981', '#075e45'],
  ['#8b5cf6', '#44238a'], ['#ef4444', '#7f1d1d'], ['#06b6d4', '#0b5563'], ['#ec4899', '#7a1d52'],
  ['#84cc16', '#3f6212'], ['#f97316', '#7c2d12'], ['#6366f1', '#2e2f86'], ['#14b8a6', '#115e59'],
];

function tone(i: number): string {
  const [a, b] = TONES[((i % TONES.length) + TONES.length) % TONES.length];
  return `linear-gradient(135deg, ${a}, ${b})`;
}

/** A colour of its own for every chart (by its id): the same each time. */
function gradient(id: string): string {
  let x = 0;
  for (const ch of id) x = (x * 31 + ch.charCodeAt(0)) >>> 0;
  return tone(x);
}
