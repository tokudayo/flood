import logging
import torch
import torchvision.transforms as T

from tqdm import tqdm

from models import EMA, create_resnet18
from utils.general import AverageMeter, load_yaml
from utils.focal_loss import BinaryFocalLossWithLogits
from utils.dataset import LabeledDataset
from utils.augment import RandAugment, WeakAugment, TrivialAugmentWide, Cutout
from models.utils import split_parameters


logger = logging.Logger(__name__)


def train():
    r"""
    Aside from the options in the config file, some parameters are hard-coded
    and need to be changed manually:
    - Resnet18 baseline
    - Augmentation policy
    - BCE loss
    - SGD optimizer
    - Cosine annealing scheduler
    """
    # Load options
    opt = load_yaml('config.yaml')
    device = 'cuda' if opt['cuda'] else 'cpu'
    use_wandb = opt['wandb']
    epochs = opt['epochs']
    batch_size = opt['batch_size']
    val_step = opt['val_step']
    use_ema = opt['ema']
    workers = opt['workers']
    sgd = opt['sgd']
    use_scheduler = opt['scheduler']
    
    # Model
    model = create_resnet18(output_nodes=1, pretrained=True)
    if use_ema:
        model = EMA(model, decay=0.9999)
    model = model.to(device)
    model.train()

    # Augmentation policy
    augment = [
        TrivialAugmentWide(),
        T.ToTensor(),
        T.Resize((224, 224)),
        T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        # Cutout(n_holes=1, length=64)
    ]

    # Datasets
    s_set = LabeledDataset(opt['train_path'], transforms=T.Compose(augment))
    val_set = LabeledDataset(opt['val_path'], transforms=T.Compose(augment))

    # Wandb logging
    if use_wandb:
        import wandb
        wandb.init(project="my-test-project", entity="toduck15hl")
        wandb.config.update(opt)
        wandb.config.update({
            'model': 'resnet18',
            'loss': 'BCE',
            'scheduler': 'CosineAnnealingLR',
            'augment': 'TrivialAugmentWide',
            'train_size': len(s_set),
        }, allow_val_change=True)

    # Dataloaders
    s_loader = torch.utils.data.DataLoader(
        s_set,
        batch_size=batch_size,
        shuffle=True,
        num_workers=workers
    )
    val_loader = torch.utils.data.DataLoader(
        val_set,
        batch_size=batch_size,
        shuffle=False,
        num_workers=workers
    )

    # Optimizer
    decay_params, no_decay_params = split_parameters(model)
    optimizer = torch.optim.SGD(
        [
            {'params': decay_params, 'weight_decay': sgd['decay']},
            {'params': no_decay_params, 'weight_decay': 0.0}
        ],
        lr=sgd['lr'], momentum=sgd['momentum'], nesterov=sgd['nesterov']
    )

    # Scheduler
    if use_scheduler:
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    # Loss
    # criterion = torch.nn.CrossEntropyLoss(torch.Tensor([0.3, 0.7]).to(device))
    # criterion = BinaryFocalLossWithLogits(alpha=0.7, gamma=2)
    criterion = torch.nn.BCEWithLogitsLoss(pos_weight=torch.Tensor([opt['pos_weight']]).to(device))

    # Grad scaler for AMP
    scaler = torch.cuda.amp.GradScaler(init_scale=2**14) # Prevents early overflow

    min_val_loss = float('inf')
    max_val_prec = 0.
    max_val_rec = 0.
    max_val_acc = 0.

    for ep in tqdm(range(1, epochs + 1)):
        epoch_loss = AverageMeter()
        for batch in tqdm(s_loader):
            # Draw a batch of labeled samples
            x_S, y_S = batch

            # AMP training
            with torch.cuda.amp.autocast():
                x_S = x_S.to(device)
                y_S = y_S.to(device)
                pred_S = model(x_S)
                loss_S = criterion(pred_S, y_S.unsqueeze(1).float())
            
            scaler.scale(loss_S).backward()
            scaler.step(optimizer)
            scaler.update()
            model.zero_grad(set_to_none=True)
            
            # Track metrics
            epoch_loss.update(loss_S.item())

        # LR scheduler
        if use_scheduler:
            scheduler.step()
        
        log_info = {'loss': epoch_loss.avg}

        # Validation
        if ep % val_step == 0:
            preds = []
            labels = []

            model.eval()
            
            with torch.no_grad():
                for batch in tqdm(val_loader):
                    x_val, y_val = batch
                    x_val = x_val.to(device)
                    y_val = y_val.to(device).unsqueeze(1).float()
                    with torch.cuda.amp.autocast(): pred = model(x_val)
                    preds.append(pred)
                    labels.append(y_val)

                # Concatenate
                preds = torch.cat(preds, dim=0)
                labels = torch.cat(labels, dim=0)

                # Compute relevant metrics
                loss = criterion(preds, labels).item()
                preds = torch.sigmoid(preds).round()
                acc = torch.mean((preds == labels).float())
                TP = (preds*labels).sum().item()
                predicted_positives = preds.sum().item()
                if predicted_positives == 0:
                    precision = 0.
                else:
                    precision = TP / (preds.sum().item())
                recall = TP / (labels.sum().item())
                
                if (
                    loss < min_val_loss or acc > max_val_acc \
                    or precision > max_val_prec or recall > max_val_rec
                ):
                    if loss < min_val_loss:
                        min_val_loss = loss
                    if acc > max_val_acc:
                        max_val_acc = acc
                    if precision > max_val_prec:
                        max_val_prec = precision
                    if recall > max_val_rec:
                        max_val_rec = recall

                torch.save(model.state_dict(), f"./weights/model_{ep}.pth")

                log_info['val_loss'] = loss
                log_info['val_acc'] = acc.item()
                log_info['val_precision'] = precision
                log_info['val_recall'] = recall

            model.train()

        if use_wandb:
                wandb.log(log_info)

    return model

        
if __name__ == "__main__":
    model = train()
    torch.save(model.state_dict(), './weights/fin.pth')