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


export const verifyImageBytes = async (body, image) => {
  const chunks = [];
  let length = 0;
  try {
    for await (const chunk of body) {
      length += chunk.length;
      if (length > 16 * 1024 * 1024) throw new Error('Editorial image exceeds 16 MiB');
      chunks.push(chunk);
    }
    const bytes = Buffer.concat(chunks);
    if (createHash('sha256').update(bytes).digest('hex') !== image.sha256) throw new Error('Editorial image checksum mismatch');
    if (bytes.length < 33 || !bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) || bytes.toString('ascii', 12, 16) !== 'IHDR' || bytes.readUInt32BE(16) !== image.width || bytes.readUInt32BE(20) !== image.height) throw new Error('Editorial image dimensions or PNG header mismatch');
  } finally { body.destroy?.(); }
};
