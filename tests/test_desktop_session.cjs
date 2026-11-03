'use strict';
// Synthetic values only. Never reads local login data or calls native storage.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {decrypt} = require('../src/desktop_session.cjs');
const payload = {version: 1, atRestSecretKey: Buffer.alloc(32, 7).toString('base64')};
const key = crypto.createHash('sha256').update(payload.atRestSecretKey).digest();
const id = crypto.createHash('sha256').update(key).digest('hex').slice(0, 16);
const prefix = Buffer.from('57422d41414400010000000557424556310000000673796d2d76310000000100000010', 'hex');
const aad = Buffer.concat([prefix, Buffer.from(id), Buffer.from([2, 0, 0])]);
function fixture() {
  const nonce = Buffer.alloc(12, 3);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, nonce);
  cipher.setAAD(aad);
  const ciphertext = Buffer.concat([cipher.update('synthetic-access-token'), cipher.final()]);
  return {suite: 1, keyId: id, nonce: nonce.toString('base64'),
    authTag: cipher.getAuthTag().toString('base64'), ciphertext: ciphertext.toString('base64')};
}
const wrap = e => ({$wbEncrypted: 1, envelope: Buffer.from(JSON.stringify(e)).toString('base64')});
test('decrypts sym-v1 authenticated field without exposing master key', () => {
  assert.equal(decrypt(wrap(fixture()), () => JSON.stringify(payload)), 'synthetic-access-token');
});
test('tampered ciphertext, tag, key id and unsupported suite are rejected', () => {
  for (const change of [{ciphertext: Buffer.alloc(22).toString('base64')},
    {authTag: Buffer.alloc(16).toString('base64')}, {keyId: '0000000000000000'},
    {suite: 2}, {extra: 'unknown'}]) {
    assert.throws(() => decrypt(wrap({...fixture(), ...change}), () => JSON.stringify(payload)));
  }
});
test('unknown scheme stops before native key lookup', () => {
  assert.throws(() => decrypt({...wrap(fixture()), scheme: 'asym-v1'}, () => assert.fail('must not get key')));
});
test('wrong master key fails authentication', () => {
  assert.throws(() => decrypt(wrap(fixture()), () => JSON.stringify({...payload, atRestSecretKey: Buffer.alloc(32, 8).toString('base64')})));
});
test('failure reasons distinguish native provider, payload, key and authentication', () => {
  const e = fixture();
  for (const [wrapper, provider, reason] of [
    [wrap(e), () => {throw Error('private-native-error');}, 'KEY_PROVIDER_UNAVAILABLE'],
    [wrap(e), () => 'not-json-private-data', 'KEY_PAYLOAD_REJECTED'],
    [wrap(e), () => JSON.stringify({...payload, atRestSecretKey: Buffer.alloc(32, 8).toString('base64')}), 'KEY_MISMATCH'],
    [wrap({...e, authTag: Buffer.alloc(16).toString('base64')}), () => JSON.stringify(payload), 'AUTHENTICATION_FAILED']
  ]) {
    assert.throws(() => decrypt(wrapper, provider), error =>
      error.sessionReason === reason && !error.message.includes('private'));
  }
});
test('CLI error emits only a fixed marker and no token or input', () => {
  const {spawnSync} = require('node:child_process');
  const path = require('node:path');
  const child = spawnSync(process.execPath,
    [path.resolve(__dirname, '../src/desktop_session.cjs'), '--private-pipe'],
    {input: JSON.stringify({privateInput: 'synthetic-secret'}), encoding: 'utf8'});
  assert.equal(child.status, 2);
  assert.equal(child.stdout, '');
  assert.equal(child.stderr, 'WB_SESSION_ERROR=DECRYPT_CHECK_FAILED\n');
});
