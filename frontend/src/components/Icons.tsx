import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

export function BrandMark(props: IconProps) {
  return (
    <svg viewBox="0 0 36 36" fill="none" aria-hidden="true" {...props}>
      <rect width="36" height="36" rx="10" fill="url(#paw-grad)" />
      <circle cx="12.5" cy="14" r="2.2" fill="#FAF8F5" />
      <circle cx="18" cy="11.5" r="2.3" fill="#FAF8F5" />
      <circle cx="23.5" cy="14" r="2.2" fill="#FAF8F5" />
      <path
        d="M18 16.5c-3.4 0-6 2.6-6 5.2 0 1.8 1.4 3 3.2 3 1.1 0 1.9-.4 2.8-.4s1.7.4 2.8.4c1.8 0 3.2-1.2 3.2-3 0-2.6-2.6-5.2-6-5.2Z"
        fill="#FAF8F5"
      />
      <path d="M27 8.5l.8 1.9 1.9.8-1.9.8-.8 1.9-.8-1.9-1.9-.8 1.9-.8.8-1.9Z" fill="#FDE68A" />
      <defs>
        <linearGradient id="paw-grad" x1="2" y1="2" x2="34" y2="34" gradientUnits="userSpaceOnUse">
          <stop stopColor="#0F766E" />
          <stop offset="1" stopColor="#134E4A" />
        </linearGradient>
      </defs>
    </svg>
  );
}

export function DashboardIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <rect x="3" y="3" width="6" height="6" rx="1.5" />
      <rect x="11" y="3" width="6" height="6" rx="1.5" />
      <rect x="3" y="11" width="6" height="6" rx="1.5" />
      <rect x="11" y="11" width="6" height="6" rx="1.5" />
    </svg>
  );
}

export function PlusCircleIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true" {...props}>
      <circle cx="10" cy="10" r="7.25" />
      <path d="M10 6.75v6.5M6.75 10h6.5" strokeLinecap="round" />
    </svg>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <circle cx="10" cy="10" r="2.75" />
      <path
        d="M10 2.75v1.6M10 15.65v1.6M4.87 4.87l1.13 1.13M14 14l1.13 1.13M2.75 10h1.6M15.65 10h1.6M4.87 15.13L6 14M14 6l1.13-1.13"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function SparklesIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <path
        d="M10 2.5l1.6 4.4 4.4 1.6-4.4 1.6L10 14.5l-1.6-4.4L4 8.5l4.4-1.6L10 2.5Z"
        strokeLinejoin="round"
      />
      <path d="M15.5 13l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8Z" strokeLinejoin="round" />
    </svg>
  );
}

export function ShieldCheckIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <path
        d="M10 2.5l6 2.5v4.7c0 3.7-2.6 6.6-6 7.8-3.4-1.2-6-4.1-6-7.8V5l6-2.5Z"
        strokeLinejoin="round"
      />
      <path d="M7.3 10.2l1.8 1.8 3.7-3.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function FolderCloudIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <path
        d="M2.75 6.25A1.75 1.75 0 014.5 4.5h3.2l1.5 1.75h6.3a1.75 1.75 0 011.75 1.75v6.25A1.75 1.75 0 0115.5 16h-11a1.75 1.75 0 01-1.75-1.75V6.25Z"
        strokeLinejoin="round"
      />
      <path d="M10 13V9.5m0 0l-1.75 1.75M10 9.5l1.75 1.75" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function RefreshIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <path
        d="M16.25 10a6.25 6.25 0 11-1.6-4.18M16.25 4v2.75H13.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function ArrowRightIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true" {...props}>
      <path d="M4.5 10h11m0 0l-4-4m4 4l-4 4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function CheckIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true" {...props}>
      <path d="M4.5 10.5l3.5 3.5 7.5-8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function GlobeIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true" {...props}>
      <circle cx="10" cy="10" r="7.25" />
      <path d="M2.75 10h14.5M10 2.75c2.2 2.2 3.25 4.7 3.25 7.25S12.2 15.05 10 17.25C7.8 15.05 6.75 12.55 6.75 10S7.8 4.95 10 2.75Z" />
    </svg>
  );
}

export function LogoutIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <path d="M7.5 16.5H5A1.75 1.75 0 013.25 14.75V5.25A1.75 1.75 0 015 3.5h2.5" strokeLinecap="round" />
      <path d="M12.75 13.5L16.25 10l-3.5-3.5M16 10H8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <circle cx="9" cy="9" r="5.75" />
      <path d="M13.5 13.5l3.5 3.5" strokeLinecap="round" />
    </svg>
  );
}

export function ExpandIcon(props: IconProps) {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" {...props}>
      <path d="M12.5 3.5h4v4M7.5 16.5h-4v-4M16.5 3.5L11.5 8.5M3.5 16.5l5-5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
