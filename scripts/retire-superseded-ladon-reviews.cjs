#!/usr/bin/env node

const APPROVED = 'APPROVED';
const CHANGES_REQUESTED = 'CHANGES_REQUESTED';

function normalizeLogin(login) {
  return String(login || '').toLowerCase();
}

function latestBotReview(reviews, botLogin) {
  const normalizedBotLogin = normalizeLogin(botLogin);
  return reviews
    .filter((review) => normalizeLogin(review.user?.login) === normalizedBotLogin)
    .sort((left, right) => Number(left.id) - Number(right.id))
    .at(-1);
}

function supersededChangeRequests(reviews, botLogin, headSha) {
  const latest = latestBotReview(reviews, botLogin);
  if (!latest || latest.state !== APPROVED || latest.commit_id !== headSha) {
    return [];
  }

  const normalizedBotLogin = normalizeLogin(botLogin);
  return reviews.filter(
    (review) =>
      normalizeLogin(review.user?.login) === normalizedBotLogin &&
      review.state === CHANGES_REQUESTED &&
      Number(review.id) < Number(latest.id),
  );
}

async function retireSupersededLadonReviews({
  github,
  owner,
  repo,
  pullNumber,
  botLogin,
  headSha,
}) {
  const reviews = await github.paginate(github.rest.pulls.listReviews, {
    owner,
    repo,
    pull_number: pullNumber,
    per_page: 100,
  });
  const superseded = supersededChangeRequests(reviews, botLogin, headSha);

  for (const review of superseded) {
    await github.rest.pulls.dismissReview({
      owner,
      repo,
      pull_number: pullNumber,
      review_id: review.id,
      message: `Superseded by Ladon approval of ${headSha}.`,
      event: 'DISMISS',
    });
  }

  return superseded.map((review) => review.id);
}

module.exports = {
  latestBotReview,
  retireSupersededLadonReviews,
  supersededChangeRequests,
};
