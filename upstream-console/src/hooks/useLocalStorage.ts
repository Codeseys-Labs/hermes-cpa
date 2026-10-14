/**
 * LocalStorage Hook
 *
 * Reads and writes through the shared UI storage: browser localStorage in the reference
 * app, per-document memory when the console is hosted in an opaque sandbox.
 */

import { useCallback, useState } from 'react';
import { getUiStorage } from '@/hosted/storage';

export function useLocalStorage<T>(
  key: string,
  initialValue: T
): [T, (value: T | ((val: T) => T)) => void] {
  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      const item = getUiStorage('local').getItem(key);
      return item ? (JSON.parse(item) as T) : initialValue;
    } catch (error) {
      console.error(`Error reading localStorage key "${key}":`, error);
      return initialValue;
    }
  });

  const setValue = useCallback(
    (value: T | ((val: T) => T)) => {
      setStoredValue((currentValue) => {
        const valueToStore = value instanceof Function ? value(currentValue) : value;

        try {
          getUiStorage('local').setItem(key, JSON.stringify(valueToStore));
        } catch (error) {
          console.error(`Error setting localStorage key "${key}":`, error);
        }

        return valueToStore;
      });
    },
    [key]
  );

  return [storedValue, setValue];
}
