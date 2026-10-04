import {createHash} from 'node:crypto';

export const verifyNarrationBytes = async (body, expectedHash) => {
  const hash = createHash('sha256');
  let length = 0;
  try {
    for await (const chunk of body) {
      length += chunk.length;
      if (length > 32 * 1024 * 1024) throw new Error('Editorial narration exceeds 32 MiB');
      hash.update(chunk);
    }
    if (!length || hash.digest('hex') !== expectedHash) throw new Error('Editorial narration checksum mismatch');
  } finally { body.destroy?.(); }
};
