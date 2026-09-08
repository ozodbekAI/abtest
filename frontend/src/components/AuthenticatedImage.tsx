import { useEffect, useState, type ImgHTMLAttributes } from 'react';
import { getStoredAccessToken } from '../api/client';

function isLocalMedia(value: string) {
  try {
    const url = new URL(value, window.location.origin);
    return url.pathname.startsWith('/media/');
  } catch {
    return false;
  }
}

/** Add the access header only for private application media; WB CDN images stay direct. */
export function AuthenticatedImage({ src, ...props }: ImgHTMLAttributes<HTMLImageElement>) {
  const [resolved, setResolved] = useState<string | undefined>(isLocalMedia(String(src || '')) ? undefined : src);

  useEffect(() => {
    const source = String(src || '');
    if (!source || !isLocalMedia(source)) {
      setResolved(src);
      return;
    }
    const token = getStoredAccessToken();
    if (!token) {
      setResolved(undefined);
      return;
    }
    const controller = new AbortController();
    let objectUrl: string | undefined;
    setResolved(undefined);
    fetch(source, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Media request failed: ${response.status}`);
        return response.blob();
      })
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        setResolved(objectUrl);
      })
      .catch(() => {
        if (!controller.signal.aborted) setResolved(undefined);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);

  return <img {...props} src={resolved} />;
}
