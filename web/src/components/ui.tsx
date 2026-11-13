import { useEffect, useState, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { goBack, navigate } from '../lib/router';
import { downloadStatusLabel, percent } from '../lib/format';
import type { OwnedStatus } from '../api/types';
import { Icon, type IconName } from './Icon';

const cx = (...xs: Array<string | false | null | undefined>) => xs.filter(Boolean).join(' ');
export { cx };

// --- buttons --------------------------------------------------------------------------------------

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';

const VARIANTS: Record<Variant, string> = {
  primary: 'bg-accent text-on-accent hover:bg-accent-strong active:scale-[0.98] font-semibold disabled:bg-raised disabled:text-dim disabled:opacity-100',
  secondary: 'bg-raised text-fg border border-line hover:bg-hover active:scale-[0.98]',
  ghost: 'text-fg hover:bg-fg/5',
  danger: 'bg-danger/15 text-danger border border-danger/30 hover:bg-danger/25',
};

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  icon?: IconName;
  busy?: boolean;
  small?: boolean;
  block?: boolean;
};

export function Button({ variant = 'secondary', icon, busy, small, block, className, children, disabled, ...rest }: BtnProps) {
  return (
    <button
      type="button"
      className={cx(
        'inline-flex select-none items-center justify-center gap-2 rounded-full transition disabled:opacity-50 disabled:pointer-events-none',
        small ? 'min-h-9 px-3.5 text-sm' : 'min-h-12 px-5 text-[15px]',
        block && 'w-full',
        VARIANTS[variant],
        className,
      )}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...rest}
    >
      {busy ? <Spinner size={small ? 16 : 18} /> : icon ? <Icon name={icon} size={small ? 18 : 20} /> : null}
      {children}
    </button>
  );
}

type IconBtnProps = ButtonHTMLAttributes<HTMLButtonElement> & { icon: IconName; label: string; busy?: boolean; size?: number; filled?: boolean };

export function IconButton({ icon, label, busy, size = 22, filled, className, disabled, ...rest }: IconBtnProps) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cx(
        'inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-fg transition hover:bg-fg/10 active:scale-95 disabled:opacity-40',
        className,
      )}
      disabled={disabled || busy}
      {...rest}
    >
      {busy ? <Spinner size={18} /> : <Icon name={icon} size={size} filled={filled} />}
    </button>
  );
}

export function Spinner({ size = 20, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className={cx('animate-spin', className)} aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

// --- states ---------------------------------------------------------------------------------------

export function Loading({ text = 'Загрузка…' }: { text?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-16 text-muted" role="status">
      <Spinner />
      <span>{text}</span>
    </div>
  );
}

export function ErrorState({ text, onRetry }: { text: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-4 px-6 py-14 text-center" role="alert">
      <Icon name="alert" size={36} className="text-danger" />
      <p className="text-muted">{text}</p>
      {onRetry && (
        <Button small icon="refresh" onClick={onRetry}>
          Повторить
        </Button>
      )}
    </div>
  );
}

export function Empty({ icon = 'note', title, text, children }: { icon?: IconName; title: string; text?: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
      <div className="flex h-16 w-16 items-center justify-center rounded-full bg-surface text-dim">
        <Icon name={icon} size={30} />
      </div>
      <p className="font-semibold">{title}</p>
      {text && <p className="max-w-xs text-sm text-muted">{text}</p>}
      {children}
    </div>
  );
}

export function SkeletonRows({ n = 6 }: { n?: number }) {
  return (
    <div className="space-y-3 px-4 py-2" aria-hidden="true">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="flex items-center gap-3">
          <div className="skeleton h-12 w-12 rounded-md" />
          <div className="flex-1 space-y-2">
            <div className="skeleton h-3.5 w-3/5 rounded" />
            <div className="skeleton h-3 w-2/5 rounded" />
          </div>
        </div>
      ))}
    </div>
  );
}

// --- layout -----------------------------------------------------------------------------------

/** `up`: «Назад» leads to `back` itself (the root of the section the page belongs to), not to the previous page of the
 * history, which may be another section: a page opened from a report of «Ещё» is closed into «Загрузки», not into the report. */
export function PageHeader({ title, back, up, right, sub }: { title: ReactNode; back?: string; up?: boolean; right?: ReactNode; sub?: ReactNode }) {
  return (
    <header className="pt-safe sticky top-0 z-20 bg-bg/90 backdrop-blur">
      <div className="flex min-h-14 items-center gap-1 px-2">
        {back !== undefined ? (
          <IconButton icon="back" label="Назад" onClick={() => (up ? navigate(back, { replace: true }) : goBack(back))} />
        ) : (
          <span className="w-2" />
        )}
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-bold">{title}</h1>
          {sub && <div className="truncate text-xs text-muted">{sub}</div>}
        </div>
        {right}
      </div>
    </header>
  );
}

export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between px-4 pb-2 pt-5">
      <h2 className="text-lg font-bold">{children}</h2>
      {right}
    </div>
  );
}

// --- badges -----------------------------------------------------------------------------------

export function ExplicitBadge() {
  return (
    <span
      className="inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-[3px] bg-muted/80 px-1 text-[10px] font-bold leading-none text-bg"
      title="Explicit"
      aria-label="Explicit"
    >
      E
    </span>
  );
}

export function OwnedBadge({ status, count, total }: { status: OwnedStatus; count?: number; total?: number | null }) {
  if (status === 'full')
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2 py-0.5 text-[11px] font-medium text-accent">
        <Icon name="check" size={12} stroke={3} />в библиотеке
      </span>
    );
  if (status === 'partial')
    return (
      <span className="inline-flex items-center rounded-full bg-warn/15 px-2 py-0.5 text-[11px] font-medium text-warn">
        частично{count != null && total ? ` ${count}/${total}` : ''}
      </span>
    );
  return null; // «нет» under every release was noise; only owned / partly owned are marked
}

const CHIP: Record<string, string> = {
  queued: 'bg-fg/8 text-muted',
  searching: 'bg-info/15 text-info',
  downloading: 'bg-info/15 text-info',
  tagging: 'bg-info/15 text-info',
  importing: 'bg-info/15 text-info',
  done: 'bg-accent/15 text-accent',
  failed: 'bg-danger/15 text-danger',
  needs_review: 'bg-warn/15 text-warn',
  cancelled: 'bg-fg/5 text-dim',
};

export function StatusChip({ status, progress = 0 }: { status: string; progress?: number }) {
  return (
    <span className={cx('inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium', CHIP[status] ?? 'bg-fg/8 text-muted')}>
      {['searching', 'downloading', 'tagging', 'importing'].includes(status) && <Spinner size={10} />}
      {downloadStatusLabel(status, progress)}
    </span>
  );
}

export function ProgressBar({ value, className }: { value: number; className?: string }) {
  return (
    <div className={cx('h-1 w-full overflow-hidden rounded-full bg-fg/10', className)} role="progressbar" aria-valuenow={percent(value)} aria-valuemin={0} aria-valuemax={100}>
      <div className="h-full rounded-full bg-accent transition-[width] duration-300" style={{ width: `${percent(value)}%` }} />
    </div>
  );
}

// --- cover image ---------------------------------------------------------------------------------

export function Cover({
  src,
  alt = '',
  size,
  round,
  className,
  icon = 'note',
}: {
  src?: string | null;
  alt?: string;
  size?: number;
  round?: boolean;
  className?: string;
  icon?: IconName;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  const style = size ? { width: size, height: size } : undefined;
  const shape = round ? 'rounded-full' : 'rounded-md';
  if (!src || failed) {
    return (
      <div className={cx('flex shrink-0 items-center justify-center bg-raised text-dim', shape, className)} style={style} role="img" aria-label={alt || undefined}>
        <Icon name={icon} size={size ? Math.max(16, Math.round(size * 0.4)) : 28} />
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
      className={cx('shrink-0 bg-raised object-cover', shape, className)}
      style={style}
    />
  );
}

// --- forms ---------------------------------------------------------------------------------------

export function Field({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <label className={cx('block', className)}>
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div role="tablist" aria-label={label} className="flex gap-2 overflow-x-auto no-scrollbar">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={value === o.value}
          onClick={() => onChange(o.value)}
          className={cx(
            'min-h-9 shrink-0 rounded-full px-4 text-sm transition',
            value === o.value ? 'bg-accent font-semibold text-on-accent' : 'bg-raised text-fg hover:bg-hover',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Checkbox({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="flex h-11 w-11 shrink-0 items-center justify-center disabled:opacity-40"
    >
      <span
        className={cx(
          'flex h-5.5 w-5.5 items-center justify-center rounded-md border-2 transition',
          checked ? 'border-accent bg-accent text-on-accent' : 'border-dim',
        )}
      >
        {checked && <Icon name="check" size={14} stroke={3.5} />}
      </span>
    </button>
  );
}

export function Radio({ checked }: { checked: boolean }) {
  return (
    <span className={cx('flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2', checked ? 'border-accent' : 'border-dim')} aria-hidden="true">
      {checked && <span className="h-2.5 w-2.5 rounded-full bg-accent" />}
    </span>
  );
}

// --- sheet (bottom modal) -------------------------------------------------------------------------

export function Sheet({ open, onClose, title, children, footer }: { open: boolean; onClose: () => void; title: string; children: ReactNode; footer?: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center" role="dialog" aria-modal="true" aria-label={title}>
      <button type="button" aria-label="Закрыть" className="absolute inset-0 bg-black/70" onClick={onClose} />
      <div className="anim-up pb-safe relative flex max-h-[88dvh] w-full max-w-lg flex-col rounded-t-2xl bg-surface sm:rounded-2xl">
        <div className="flex items-center gap-2 border-b border-line px-4 py-2">
          <h2 className="flex-1 truncate text-base font-bold">{title}</h2>
          <IconButton icon="x" label="Закрыть" onClick={onClose} />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">{children}</div>
        {footer && <div className="border-t border-line px-4 py-3">{footer}</div>}
      </div>
    </div>
  );
}

export function Notice({ kind = 'warn', children }: { kind?: 'warn' | 'error' | 'info'; children: ReactNode }) {
  const color = kind === 'error' ? 'text-danger bg-danger/10' : kind === 'info' ? 'text-info bg-info/10' : 'text-warn bg-warn/10';
  return (
    <div className={cx('flex items-start gap-2 rounded-lg px-3 py-2 text-sm', color)}>
      <Icon name="alert" size={18} className="mt-0.5 shrink-0" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}
