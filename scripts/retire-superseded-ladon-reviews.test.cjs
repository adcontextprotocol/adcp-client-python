#!/usr/bin/env node

const assert = require('node:assert/strict');
const test = require('node:test');

const {
  retireSupersededLadonReviews,
  supersededChangeRequests,
} = require('./retire-superseded-ladon-reviews.cjs');

const BOT = 'aao-secretariat[bot]';
const HEAD = 'new-head';

function review(id, state, commitId = HEAD, login = BOT) {
  return { id, state, commit_id: commitId, user: { login } };
}

test('selects only the bot change requests superseded by its final head approval', () => {
  const reviews = [
    review(10, 'CHANGES_REQUESTED', 'old-head'),
    review(11, 'CHANGES_REQUESTED'),
    review(12, 'CHANGES_REQUESTED', 'old-head', 'human-reviewer'),
    review(13, 'DISMISSED', 'old-head'),
    review(14, 'APPROVED'),
  ];

  assert.deepEqual(
    supersededChangeRequests(reviews, 'AAO-SECRETARIAT[BOT]', HEAD).map(
      ({ id }) => id,
    ),
    [10, 11],
  );
});

test('keeps change requests when the latest bot review is not an approval', () => {
  const reviews = [
    review(20, 'CHANGES_REQUESTED', 'old-head'),
    review(21, 'APPROVED'),
    review(22, 'COMMENTED'),
  ];

  assert.deepEqual(supersededChangeRequests(reviews, BOT, HEAD), []);
});

test('keeps change requests when the latest approval targets an older head', () => {
  const reviews = [
    review(30, 'CHANGES_REQUESTED', 'older-head'),
    review(31, 'APPROVED', 'previous-head'),
  ];

  assert.deepEqual(supersededChangeRequests(reviews, BOT, HEAD), []);
});

test('dismisses every selected review through the pull request API', async () => {
  const dismissed = [];
  const listReviews = Symbol('listReviews');
  const github = {
    paginate: async (method, params) => {
      assert.equal(method, listReviews);
      assert.deepEqual(params, {
        owner: 'adcontextprotocol',
        repo: 'adcp-client-python',
        pull_number: 1134,
        per_page: 100,
      });
      return [
        review(40, 'CHANGES_REQUESTED', 'old-head'),
        review(41, 'APPROVED'),
      ];
    },
    rest: {
      pulls: {
        listReviews,
        dismissReview: async (params) => dismissed.push(params),
      },
    },
  };

  const ids = await retireSupersededLadonReviews({
    github,
    owner: 'adcontextprotocol',
    repo: 'adcp-client-python',
    pullNumber: 1134,
    botLogin: BOT,
    headSha: HEAD,
  });

  assert.deepEqual(ids, [40]);
  assert.deepEqual(dismissed, [
    {
      owner: 'adcontextprotocol',
      repo: 'adcp-client-python',
      pull_number: 1134,
      review_id: 40,
      message: `Superseded by Ladon approval of ${HEAD}.`,
      event: 'DISMISS',
    },
  ]);
});
