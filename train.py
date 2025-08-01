import os
import tiktoken
import torch
import torch.nn as nn
from torch.nn import ReLU, functional as F
from tqdm import tqdm

# hyperparameters
block_size = 256
max_iters = 5000
eval_interval = 200
device = 'cuda' if torch.cuda.is_available() else 'mps'
eval_iters = 200
n_layers = 6
n_embd = 384 # also known as d_model
n_heads = 6
batch_size = 64
learning_rate = 3e-4
dropout = 0.2 # 20% of dropout: A simple way to prevent NN from overfitting

# ------------

# Configurations
should_train = False # set to True to train the model. If a checkpoint exists, it will resume training from there.
always_save_checkpoint = True
always_save_checkpoint_every_n_iters = 100
always_save_checkpoint_path = "checkpoints/model.tar"

#----

# one-hot encoding
torch.manual_seed(1337)

# ------------

# read the input.txt file
with open('input.txt', 'r', encoding='utf-8') as f:
  text = f.read()

# here are all the unique characters that occur in this text
chars = sorted(list(set(text)))
vocab_size = len(chars)

# create a mapping from characters to integers
stoi = { ch:i for i, ch in enumerate(chars) }
itos = { i:ch for i, ch in enumerate(chars) }
encode = lambda s: [stoi[c] for c in s] # encoder: take a string, output a list of integers
decode = lambda l: ''.join([itos[i] for i in l]) # decoder: take a list of integers, output a string

# Split the data into train and validation sets
data = torch.tensor(encode(text), dtype=torch.long)
n = int(0.9*len(data)) # first 90% will be training, the rest for evaluation
train_data = data[:n]
val_data = data[n:]

# data loading
def get_batch(split):
  # generate a small batch of data of inputs x and targets y
  data = train_data if split == 'train' else val_data
  ix = torch.randint(len(data) - block_size, (batch_size,))
  x = torch.stack([data[i:i+block_size] for i in ix])
  y = torch.stack([data[i+1:i+block_size+1] for i in ix])
  x, y = x.to(device), y.to(device)
  return x, y

@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            logits, loss= model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out

class Head(nn.Module):
  """ one head of self-attention """

  def __init__(self, head_size):
    super().__init__()
    self.key = nn.Linear(n_embd, head_size, bias=False)
    self.query = nn.Linear(n_embd, head_size, bias=False)
    self.value = nn.Linear(n_embd, head_size, bias=False)
    self.register_buffer('tril', torch.tril(torch.ones(block_size, block_size)))
    self.dropout = nn.Dropout(dropout)

  def forward(self, x):
    B,T,C = x.shape
    k = self.key(x) # (B, T, C)
    q = self.query(x) # (B, T, C)

    # compute attention scores ("affinities")
    wei = q @ k.transpose(-2, -1) * C**-0.5 # (B, T, 16) @ (B, 16, T) -> (B, T, T)
    wei = wei.masked_fill(self.tril[:T, :T] == 0, float('-inf')) # (B, T, T)
    wei = F.softmax(wei, dim=-1)
    wei = self.dropout(wei)
    v = self.value(x) # (B, T, C)
    out = wei @ v
    return out

class MultiHeadAttention(nn.Module):
  """ multiple heads of self-attention in parallel """

  def __init__(self, num_heads, head_size):
    super().__init__()
    self.heads = nn.ModuleList([Head(head_size) for _ in range(num_heads)])
    self.projection = nn.Linear(n_embd, n_embd)
    self.dropout = nn.Dropout(dropout)

  def forward(self, x):
    out = torch.cat([h(x) for h in self.heads], dim=-1)
    out = self.dropout(self.projection(out))
    return out

class FeedForward(nn.Module):
  """a simple linear layer followed by a non-linearity"""

  def __init__(self, n_embd):
    super().__init__()
    self.net = nn.Sequential (
      nn.Linear(n_embd, 4 * n_embd),
      nn.ReLU(),
      nn.Linear(4 * n_embd, n_embd),
      nn.Dropout(dropout)
    )

  def forward(self, x):
    return self.net(x)


class Block(nn.Module):
  """ transformer block without cross-communication: communication by computation """
  def __init__(self, n_embd, n_head):
    super().__init__()
    head_size = n_embd // n_head
    self.sa = MultiHeadAttention(n_head, head_size)
    self.ffwd = FeedForward(n_embd)
    self.ln1 = nn.LayerNorm(n_embd)
    self.ln2 = nn.LayerNorm(n_embd)

  def forward(self, x):
    x = x + self.sa(self.ln1(x))
    x = x + self.ffwd(self.ln2(x))
    return x


# Simple Bigram Language Model
class BigramLanguageModel(nn.Module):

  def __init__(self, vocab_size):
    super().__init__()
    # each token directly reads off the logits for the next 
    # token from the lookup table
    self.token_embedding_table = nn.Embedding(vocab_size, n_embd)
    self.position_embedding_table = nn.Embedding(block_size, n_embd)
    self.lm_head = nn.Linear(n_embd, vocab_size)
    self.blocks = nn.Sequential(*[Block(n_embd, n_head=n_heads) for _ in range(n_layers)])
    self.ln_f = nn.LayerNorm(n_embd) # final layer norm

  def forward(self, idx, targets=None):
    B, T = idx.shape

    # idx and targets are both (B, T) tensor of integers
    tok_embd = self.token_embedding_table(idx) # (B,T,C)
    pos_embd = self.position_embedding_table(torch.arange(T, device=device)) # (T, C)
    x = tok_embd + pos_embd # (B, T, C)
    x = self.blocks(x) # (B,T,C)
    x = self.ln_f(x) # (B,T,C) apply the final layer normalisation
    logits = self.lm_head(x) # (B, T, vocab_size)

    if targets is None:
      loss = None
    else:
      B, T, C = logits.shape
      # reshape so B, T is 1D array instead of 2D. This is because
      # pytorch F.cross_entropy expects channels as the second argument.
      logits = logits.view(B*T, C)
      targets = targets.view(-1) # -1 is same as B*T
      loss = F.cross_entropy(logits, targets)
    return logits, loss

  def generate(self, idx, max_new_tokens):
    # idx is (B, T) array of indices in the current context
    for _ in range(max_new_tokens):
      # crop idx to the last block_size tokens
      idx_cond = idx[:, -block_size:]

      # get the predictions
      logits, loss = self(idx_cond)
      # focus only on the last time step
      logits = logits[:, -1, :] # becomes (B, C)
      # apply softmax to get probabilities
      probs = F.softmax(logits, dim=1) # (B, C)
      #sample from the distribution
      idx_next = torch.multinomial(probs, num_samples=1) # (B, 1)
      # append sampled index to the running sequence
      idx = torch.cat((idx, idx_next), dim=1) # (B, T+1)
    return idx

model = BigramLanguageModel(vocab_size)
m = model.to(device)

# Print the number of parameters
print('Model size is', sum(p.numel() for p in m.parameters())/1e6, 'M paramaters')

# Create our PyTorch optimiser
optimizer = torch.optim.AdamW(m.parameters(), lr=learning_rate)

# Load the model if it exists
if os.path.exists(always_save_checkpoint_path):
    print(f"Resuming training from {always_save_checkpoint_path}")
    checkpoint = torch.load(always_save_checkpoint_path)
    m.load_state_dict(checkpoint)
    # optimizer.load_state_dict(checkpoint['optimizer'])

# Train the transformer
if should_train:
  print("Training...")
  for iter in tqdm(range(max_iters)):
      # every once in a while evaluate the loss on train and val sets
      if iter % eval_interval == 0:
          losses = estimate_loss()
          print(f"step {iter}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")

          # Save the model
          if always_save_checkpoint and iter % always_save_checkpoint_every_n_iters == 0:
              os.makedirs(os.path.dirname(always_save_checkpoint_path), exist_ok=True)
              torch.save(model.state_dict(), always_save_checkpoint_path)
              print(f"Model saved to {always_save_checkpoint_path}") 
      
      # sample a batch of data
      xb, yb = get_batch('train')

      # evaluate the loss
      logits, loss = model(xb, yb)
      optimizer.zero_grad(set_to_none=True)
      loss.backward()
      optimizer.step()

# generate from the model
print("Generating...")
context = torch.zeros((1, 1), dtype=torch.long, device=device)
print(decode(m.generate(context, max_new_tokens=1000)[0].tolist()))
